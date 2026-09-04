"""Topic identity, and the note we write about one.

The hard part is not rendering — it is making sure that when this app and
`podcast-digest` both mean CrowdStrike, they land on the same file. They do not
talk to each other; they agree only by both applying the same two functions to a
name. So :func:`canonical` and :func:`slugify` are **ported verbatim** from
`podcast_agent/entities.py` and `podcast_agent/sanitize.py`. Changing either here
alone produces ``crowd-strike.md`` beside ``crowdstrike.md`` and a graph that
splits one subject in two.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from .clippings import Clipping
from .notes import KEY_PREFIX, wrap

# ── identity: ported verbatim, do not "improve" in one copy ──────────────────

_CVE = re.compile(r"^cve[\s\-_]*(\d{4})[\s\-_]*(\d{4,7})$", re.IGNORECASE)
_LEADING = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_TRAILING = re.compile(r"[\s,]+(inc|inc\.|llc|ltd|ltd\.|corp|corp\.|gmbh|plc)$", re.IGNORECASE)
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def canonical(name: str) -> str:
    """The key two spellings of the same thing must share.

    Deliberately conservative. Over-merging is the worse error: it silently
    fuses two unrelated entities into one timeline that reads as evidence, and
    nothing downstream can tell. Under-merging leaves two rows a reader can see
    and interpret for themselves.
    """
    text = " ".join(str(name).split()).strip(" .,;:—-")
    if not text:
        return ""
    if match := _CVE.match(text):
        return f"cve-{match.group(1)}-{int(match.group(2)):04d}"
    text = _LEADING.sub("", text)
    text = _TRAILING.sub("", text)
    return text.casefold().strip(" .,;:—-")


def slugify(text: str, *, max_len: int = 60, fallback: str = "untitled") -> str:
    """ASCII-only, lowercase, hyphenated slug — the topic note's filename."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or fallback


# ── aggregation ───────────────────────────────────────────────────────────────


@dataclass
class Topic:
    key: str  # canonical()
    surfaces: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    clippings: list[Clipping] = field(default_factory=list)

    @property
    def name(self) -> str:
        """The spelling to display: the commonest, ties broken by the longest.

        Same rule as podcast-digest's `display_name` — longer is usually more
        informative ("Volt Typhoon" over "Volt") — with one addition. A model
        asked for topic names tends to answer in lowercase, and for a topic that
        exists only in clippings there is no better surface to fall back on. So a
        name that is *only* ever seen lowercase and is purely alphabetic gets its
        first letter capitalised: "ollama" → "Ollama". Anything with a digit or
        punctuation is left exactly as written, which is what keeps "n8n" from
        becoming "N8n".

        Only ever affects notes this app creates: `title` is create-only, so an
        existing note keeps whatever it already says.
        """
        best = max(self.surfaces.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        if best.islower() and best.isalpha():
            return best[:1].upper() + best[1:]
        return best

    @property
    def slug(self) -> str:
        return slugify(self.name) or self.key

    @property
    def dates(self) -> list[str]:
        return sorted(c.date for c in self.clippings if c.date)


def aggregate(extracted: dict[str, list[str]], by_id: dict[str, Clipping]) -> dict[str, Topic]:
    """``{clipping doc_id: [topic names]}`` → topics, keyed canonically."""
    topics: dict[str, Topic] = {}
    for doc_id, names in sorted(extracted.items()):
        clipping = by_id.get(doc_id)
        if clipping is None:
            continue  # extracted before the clipping was deleted; it is gone now
        for name in names:
            key = canonical(name)
            if not key:
                continue
            topic = topics.setdefault(key, Topic(key=key))
            topic.surfaces[name.strip()] += 1
            if clipping not in topic.clippings:
                topic.clippings.append(clipping)
    return topics


def distribution(topics: dict[str, Topic]) -> list[tuple[int, int]]:
    """``[(threshold, notes that would exist)]`` — the calibration table.

    podcast-digest records its own in `config.yaml` (2 → 1269, 5 → 297, 8 → 137)
    and runs at 8. That is over thousands of episodes; this corpus is two orders
    of magnitude smaller, so its number has to be measured, not inherited.
    """
    counts = sorted((len(t.clippings) for t in topics.values()), reverse=True)
    return [(n, sum(1 for c in counts if c >= n)) for n in (1, 2, 3, 4, 5, 8, 12)]


# ── the note ──────────────────────────────────────────────────────────────────


def render(topic: Topic) -> str:
    """A complete topic note as we would write it fresh.

    The merge takes only our region and our prefixed frontmatter out of this;
    the rest is a seed used when the note does not exist yet.

    Newest first, and every field sorted or derived — unchanged input must
    produce a byte-identical note, or every run re-replicates the vault.
    """
    lines = sorted(
        topic.clippings,
        key=lambda c: (c.date or "0000-00-00", c.title),
        reverse=True,
    )
    body = "## From clippings\n\n" + "\n".join(
        f"- {c.date} · {c.link}" if c.date else f"- {c.link}" for c in lines
    )

    dates = topic.dates
    front = [
        "type: topic",
        f'title: "{topic.name}"',
        "tags: [topic]",
        f"{KEY_PREFIX}count: {len(topic.clippings)}",
    ]
    if dates:
        front.append(f"{KEY_PREFIX}first_seen: {dates[0]}")
        front.append(f"{KEY_PREFIX}last_seen: {dates[-1]}")

    return (
        "---\n"
        + "\n".join(front)
        + "\n---\n\n"
        + f"# {topic.name}\n\n"
        + wrap(body)
        + "\n"
    )


def render_empty(topic_name: str) -> str:
    """A topic that has no clippings any more.

    The region is left in place but **empty**, and the ``clip_*`` keys drop away
    (the merge removes prefixed keys a run no longer produces). The note itself
    stays: deleting a note is the reader's call, and another writer may own a
    region in it.

    Empty rather than a "no clippings currently mention this" line, which is what
    this wrote first. That sentence is invisible to nobody — it lands on someone
    else's topic page and says nothing useful, and 18 notes acquired one the
    first time two extraction runs disagreed about a marginal topic. An empty
    marked region renders as nothing at all, and still lets the next run put its
    list back in the same place.
    """
    return (
        "---\n"
        + "\n".join(["type: topic", f'title: "{topic_name}"', "tags: [topic]"])
        + "\n---\n\n"
        + f"# {topic_name}\n\n"
        + wrap("")
        + "\n"
    )
