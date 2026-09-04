"""Map clippings to topic notes.

    python -m clippings_topics                 read, extract, write, exit
    python -m clippings_topics --serve         the same, weekly, forever
    python -m clippings_topics --dry-run       everything except the writes
    python -m clippings_topics --calibrate     the min_mentions table, then stop
    python -m clippings_topics --only anthropic   one topic, for a first run
    python -m clippings_topics --reap-only     just clear duplicated notes

Failure is never fatal to the corpus: a clipping the model would not label is
skipped and retried next run, and a vault that is down raises before anything is
written.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
import sys
import time
from pathlib import Path

from . import clippings as clip
from . import topics as top
from .extract import DEFAULT_MODEL, Cache, Extractor
from .extract import resolve as resolve_topics
from .janitor import reap as reap_duplicates
from .notes import begin_marker
from .vault import LiveSyncVault, VaultConfig, VaultUnavailable

log = logging.getLogger("clippings-topics")

RAW_PREFIX = "10 raw/"
TOPICS_PREFIX = "99 topics/"
TOPICS_FOLDER = "99 topics"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="clippings-topics")
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--calibrate", action="store_true", help="print the threshold table")
    parser.add_argument("--only", metavar="SLUG", help="write just this one topic")
    parser.add_argument(
        "--reap-only",
        action="store_true",
        help="just clear the duplicate notes two sync systems made, then stop",
    )
    parser.add_argument(
        "--min-mentions",
        type=int,
        default=int(os.environ.get("CLIP_MIN_MENTIONS", "3")),
        help="clippings a NEW topic needs before it gets a note (default 3)",
    )
    parser.add_argument("--cache", default=os.environ.get("CLIP_CACHE", "data/cache.json"))
    parser.add_argument(
        "--serve",
        action="store_true",
        help="run on a schedule instead of once (what the container does)",
    )
    parser.add_argument(
        "--at",
        default=os.environ.get("CLIP_RUN_AT", "sat 06:30"),
        help="weekly slot for --serve, e.g. 'sat 06:30' (local time)",
    )
    return parser.parse_args()


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _seconds_until(spec: str) -> float:
    """Seconds to the next weekly occurrence of ``"sat 06:30"``.

    A hand-rolled weekly slot rather than a cron dependency: there is exactly one
    schedule, and `croniter` for one line of config is a dependency to patch
    forever. Deliberately after podcast-digest's Friday vault rebuild — both
    writers merge into the same topic notes, and while the merge is safe
    concurrently, a run that reads a note mid-rewrite does needless work.
    """
    day, _, clock = spec.strip().lower().partition(" ")
    hour, _, minute = clock.partition(":")
    target_dow = _DAYS.index(day) if day in _DAYS else 5
    now = datetime.datetime.now()
    nxt = now.replace(hour=int(hour or 6), minute=int(minute or 30), second=0, microsecond=0)
    ahead = (target_dow - nxt.weekday()) % 7
    nxt += datetime.timedelta(days=ahead)
    if nxt <= now:
        nxt += datetime.timedelta(days=7)
    return (nxt - now).total_seconds()


async def run() -> int:
    args = _args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx logs a line per request; this job makes hundreds of small reads.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    url = os.environ.get("VAULT_COUCHDB_URL", "")
    if not url:
        log.error("VAULT_COUCHDB_URL is not set")
        return 2

    vault = LiveSyncVault(
        VaultConfig(
            couchdb_url=url,
            db=os.environ.get("VAULT_DB", "tastings"),
            user=os.environ.get("VAULT_USER", "admin"),
        ),
        os.environ.get("VAULT_COUCHDB_PASSWORD"),
    )

    try:
        # 0. Clear the copies iCloud and LiveSync make of each other's new
        #    notes. First, so a `delta 2.md` is not counted as an existing topic
        #    for the rest of the run. Byte-identical copies only — see janitor.
        reaped, findings = await reap_duplicates(vault, dry_run=args.dry_run)
        if args.reap_only:
            for finding in findings:
                print(f"  {finding.verdict:<10} {finding.path}")
            print(f"\n  reaped {reaped} of {len(findings)}")
            return 0

        # 1. What is in the vault right now. Soft-deleted notes are filtered in
        #    list_prefix — the difference between 109 clippings and 141 rows.
        found = await clip.load(vault, RAW_PREFIX)
        existing_notes = await vault.list_prefix(TOPICS_PREFIX)
        existing_slugs = {
            e.path.rsplit("/", 1)[-1].removesuffix(".md").lower() for e in existing_notes
        }
        log.info("loaded clippings=%d existing_topics=%d", len(found), len(existing_slugs))

        # 2. Topics, from cache where the text has not changed.
        cache = Cache(Path(args.cache))
        forgotten = cache.keep_only({c.doc_id for c in found})
        if forgotten:
            log.info("cache.forgot gone=%d", forgotten)

        api_key = os.environ.get("OPENROUTER_API_KEY")
        extractor = (
            Extractor(api_key, os.environ.get("CLIP_MODEL", DEFAULT_MODEL))
            if api_key
            else None
        )
        if extractor is None:
            log.warning("OPENROUTER_API_KEY not set — using cached topics only")

        # The vocabulary to prefer: what the vault already calls things, taken
        # from each note's `title:` and not from its filename. Feeding slugs
        # instead taught the model to answer in slugs — every topic came back
        # lowercased, including ones the vault spells "Anthropic" and
        # "CrowdStrike". Read once here and reused for the emptying pass below,
        # so it costs one sweep of the folder rather than two.
        current_notes: dict[str, tuple[object, str]] = {}
        for entry in existing_notes:
            markdown = await vault.read(entry)
            if markdown is not None:
                current_notes[_title_of(entry.path).lower()] = (entry, markdown)
        known = sorted(
            {_note_title(md) or _title_of(e.path) for e, md in current_notes.values()},
            key=str.lower,
        )
        try:
            extracted = await resolve_topics(found, cache, extractor, known)
        finally:
            if extractor is not None:
                await extractor.close()
            # Saved HERE, not per-branch. --calibrate used to return before its
            # save and discard 99 extractions it had just paid for; extraction is
            # the only expensive thing this job does, so it is banked the moment
            # it exists, whatever the caller asked for.
            cache.save()

        by_id = {c.doc_id: c for c in found}
        topics = top.aggregate(extracted, by_id)
        log.info("topics distinct=%d", len(topics))

        # 3. Which ones get a note.
        table = top.distribution(topics)
        if args.calibrate:
            print("\n  min_mentions → topic notes that would exist")
            for threshold, count in table:
                marker = "  ← default" if threshold == args.min_mentions else ""
                print(f"    {threshold:>2} → {count:>4}{marker}")
            print(
                f"\n  {sum(1 for t in topics.values() if t.slug in existing_slugs)}"
                f" of {len(topics)} already have a note; those are written at any threshold."
            )
            return 0

        selected = {
            key: topic
            for key, topic in topics.items()
            # An existing note is enriched however few clippings mention it: the
            # threshold exists to stop us *creating* near-empty pages, not to
            # withhold a link from a page that is already there.
            if topic.slug in existing_slugs or len(topic.clippings) >= args.min_mentions
        }
        if args.only:
            selected = {k: t for k, t in selected.items() if t.slug == args.only}
            if not selected:
                log.error("no topic with slug %r", args.only)
                return 2

        created = sum(1 for t in selected.values() if t.slug not in existing_slugs)
        log.info(
            "writing topics=%d (new=%d, enriched=%d) dry_run=%s",
            len(selected),
            created,
            len(selected) - created,
            args.dry_run,
        )

        if args.dry_run:
            for topic in sorted(selected.values(), key=lambda t: -len(t.clippings))[:15]:
                new = " NEW" if topic.slug not in existing_slugs else ""
                log.info("  %-34s %3d clippings%s", topic.slug, len(topic.clippings), new)
            return 0

        # 4. Merge. Read-then-write per note, against the vault.
        now_ms = int(time.time() * 1000)
        written = 0
        for key in sorted(selected):
            topic = selected[key]
            path = f"{TOPICS_FOLDER}/{topic.slug}.md"
            if await vault.project(path, top.render(topic), mtime_ms=now_ms, merge=True):
                written += 1

        # A topic that had clippings last run and none now keeps its note, but
        # our region is emptied and the clip_* keys drop away.
        emptied = 0
        if not args.only:
            written_slugs = {t.slug for t in selected.values()}
            for slug, (entry, current) in sorted(current_notes.items()):
                if slug in written_slugs:
                    continue
                # Our MARKER, not our frontmatter prefix. Emptying a note drops
                # its clip_* keys, so a prefix check made an emptied note
                # untouchable ever after — including for fixing what we left in
                # it. The marker is the thing that says we own a region here.
                if begin_marker() not in current:
                    continue  # we never wrote here; not ours to empty
                body = top.render_empty(_note_title(current) or _title_of(entry.path))
                if await vault.project(entry.path, body, mtime_ms=now_ms, merge=True):
                    emptied += 1

        log.info("done written=%d emptied=%d", written, emptied)
        return 0
    except VaultUnavailable as exc:
        log.error("vault unavailable: %s", exc)
        return 1
    finally:
        await vault.close()


def _title_of(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def _note_title(markdown: str) -> str:
    """A note's `title:` frontmatter value, if it has one.

    Line-by-line, like everything else that touches frontmatter here: a YAML
    parser would reformat the human's file as the price of reading one key.
    """
    if not markdown.startswith("---\n"):
        return ""
    end = markdown.find("\n---\n", 4)
    for line in markdown[4 : end if end > 0 else len(markdown)].split("\n"):
        key, _, value = line.partition(":")
        if key.strip() == "title":
            return value.strip().strip('"').strip("'")
    return ""


#: How long to wait before retrying a failed run, and how many times, before
#: giving up until the next weekly slot.
_RETRY_S = 300
_RETRIES = 5


async def serve(spec: str) -> int:
    """Run now, then once a week. The container's entrypoint.

    Runs immediately on start so a deploy takes effect without waiting for the
    slot — but a run at startup can lose a race with its own network. Observed on
    the first deploy: the container came up, the job ran, and the vault was
    unreachable because the macvlan interface was not ready yet. It was fine
    seconds later.

    So a failed run is retried in minutes rather than deferred to the next slot.
    Without this, one transient failure at startup costs a week of not running,
    and the log line that explains it ("sleeping 89.5h") looks like normal
    operation.
    """
    while True:
        for attempt in range(1, _RETRIES + 1):
            try:
                if await run() == 0:
                    break
                log.warning("run reported failure (attempt %d/%d)", attempt, _RETRIES)
            except Exception:  # noqa: BLE001 - a scheduled job must outlive its runs
                log.exception("run raised (attempt %d/%d)", attempt, _RETRIES)
            if attempt < _RETRIES:
                log.info("retrying in %ds", _RETRY_S)
                await asyncio.sleep(_RETRY_S)
        delay = _seconds_until(spec)
        log.info("sleeping %.1fh until the next run", delay / 3600)
        await asyncio.sleep(delay)


def main() -> None:
    if "--serve" in sys.argv:
        # argparse still parses the rest inside run(); this only picks the mode.
        spec = _args().at
        sys.exit(asyncio.run(serve(spec)))
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
