"""Reaping the copies two sync systems make of one note.

Not a duplicate *topic* problem — a duplicate *file* problem, and it is not
caused by any writer here. The vault is replicated twice over: iCloud syncs the
folder while LiveSync syncs the same notes through CouchDB. When a server-side
writer **creates** a note, a LiveSync client goes to write the file and finds
the path already occupied by the copy the other channel delivered; Obsidian's
create-if-exists appends ``" 2"``, and the client then uploads that as a fresh
document. 54 notes in this vault, accumulating since 2026-07-27.

The signature is unambiguous in CouchDB. Both documents sit at rev 1 — this can
only happen at creation, never on update — and they are chunked differently:

===================  ======  ==========================================
``delta.md``         1 chunk ``h:t51e43f70…``   our server-side writer
``delta 2.md``       2       ``h:25imzxac3eyaw``  the LiveSync client
===================  ======  ==========================================

Nothing a writer does can prevent it: the copy is made client-side, after the
write has already succeeded correctly. So this cleans up after it instead, on
the same weekly pass that does everything else.

**The rule is byte-identity, not the name.** ``10 raw/`` legitimately holds
``" 1"`` and ``" 2"`` files — the Web Clipper disambiguating genuinely different
articles that share a title, some of them linked. A copy is only reaped when it
is byte-for-byte its base; anything that differs is reported and left for a
person, because two files that disagree is a question this program cannot answer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .vault import LiveSyncVault

log = logging.getLogger(__name__)

#: ``some note 2.md`` — Obsidian's disambiguation suffix.
_DUP_SUFFIX = re.compile(r"^(?P<stem>.+) (?P<n>\d+)\.md$")

#: Where a server-side writer creates notes, and therefore the only folders
#: where this failure can occur. Scoped rather than vault-wide as a second line
#: of defence behind the byte-identity rule: ``10 raw/`` is the reader's and the
#: Web Clipper's, and nothing here should be forming opinions about it.
DEFAULT_PREFIXES = ("99 topics/", "12 daily-digest/")

IDENTICAL = "identical"
DIFFERS = "differs"
NO_BASE = "no-base"
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Finding:
    path: str
    base: str
    verdict: str


async def survey(
    vault: LiveSyncVault, prefixes: tuple[str, ...] = DEFAULT_PREFIXES
) -> list[Finding]:
    """Every ``" N.md"`` note under ``prefixes``, judged against its base."""
    findings: list[Finding] = []
    for prefix in prefixes:
        entries = await vault.list_prefix(prefix)
        by_id = {e.doc_id: e for e in entries}
        for entry in entries:
            match = _DUP_SUFFIX.match(entry.doc_id)
            if match is None:
                continue
            base_id = f"{match.group('stem')}.md"
            base = by_id.get(base_id)
            if base is None:
                findings.append(Finding(entry.path, base_id, NO_BASE))
                continue
            copy_md = await vault.read(entry)
            base_md = await vault.read(base)
            if copy_md is None or base_md is None:
                # A torn note: some chunk is missing. Never delete on the
                # strength of a read that did not fully succeed.
                findings.append(Finding(entry.path, base.path, UNREADABLE))
                continue
            verdict = IDENTICAL if copy_md == base_md else DIFFERS
            findings.append(Finding(entry.path, base.path, verdict))
    return sorted(findings, key=lambda f: f.path)


async def reap(
    vault: LiveSyncVault,
    prefixes: tuple[str, ...] = DEFAULT_PREFIXES,
    *,
    dry_run: bool = False,
) -> tuple[int, list[Finding]]:
    """Soft-delete the byte-identical copies. ``(reaped, all findings)``.

    Deleted through the vault, not on disk: the copy exists on every device, and
    only the document the clients replicate can take it off all of them.
    """
    findings = await survey(vault, prefixes)
    reaped = 0
    for finding in findings:
        if finding.verdict != IDENTICAL:
            log.info("janitor.kept path=%s verdict=%s", finding.path, finding.verdict)
            continue
        if dry_run:
            log.info("janitor.would_reap path=%s base=%s", finding.path, finding.base)
            continue
        if await vault.soft_delete(finding.path.lower()):
            reaped += 1
    log.info(
        "janitor.done reaped=%d kept=%d dry_run=%s",
        reaped,
        sum(1 for f in findings if f.verdict != IDENTICAL),
        dry_run,
    )
    return reaped, findings
