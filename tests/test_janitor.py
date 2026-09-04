"""The reaper must never delete two files that disagree.

Everything else here is bookkeeping. Deleting a copy that is byte-identical to
its base loses nothing; deleting one that differs destroys the only place some
content exists, on every device at once, and looks like Obsidian losing a note.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clippings_topics import janitor  # noqa: E402
from clippings_topics.vault import Entry  # noqa: E402

BASE = "---\ntype: topic\n---\n\n# Delta\n"


class FakeVault:
    """Enough vault to exercise the janitor: paths, contents, deletions."""

    def __init__(self, notes: dict[str, str]) -> None:
        self.notes = dict(notes)
        self.deleted: list[str] = []
        self.torn: set[str] = set()

    async def list_prefix(self, prefix: str) -> list[Entry]:
        return [
            Entry(doc_id=p.lower(), path=p, rev="1-a", deleted=False, children=("c",), mtime=0)
            for p in sorted(self.notes)
            if p.lower().startswith(prefix)
        ]

    async def read(self, entry: Entry) -> str | None:
        if entry.path in self.torn:
            return None
        return self.notes[entry.path]

    async def soft_delete(self, doc_id: str) -> bool:
        self.deleted.append(doc_id)
        return True


PREFIXES = ("99 topics/",)


def reap(vault: FakeVault, **kw: object) -> tuple[int, list[janitor.Finding]]:
    """Driven synchronously: seven tests are not worth a pytest-asyncio dependency."""
    return asyncio.run(janitor.reap(vault, PREFIXES, **kw))  # type: ignore[arg-type]


def test_identical_copy_is_reaped() -> None:
    vault = FakeVault({"99 topics/delta.md": BASE, "99 topics/delta 2.md": BASE})
    reaped, _ = reap(vault)
    assert reaped == 1
    assert vault.deleted == ["99 topics/delta 2.md"]


def test_a_copy_that_differs_is_never_deleted() -> None:
    """The one that matters. Two files disagreeing is a question for a human."""
    vault = FakeVault(
        {"99 topics/delta.md": BASE, "99 topics/delta 2.md": BASE + "\nand a human's line\n"}
    )
    reaped, findings = reap(vault)
    assert reaped == 0
    assert vault.deleted == []
    assert [f.verdict for f in findings] == [janitor.DIFFERS]


def test_the_base_is_never_the_thing_deleted() -> None:
    vault = FakeVault({"99 topics/delta.md": BASE, "99 topics/delta 2.md": BASE})
    reap(vault)
    assert "99 topics/delta.md" not in vault.deleted


def test_a_numbered_note_with_no_base_is_left_alone() -> None:
    """`10 raw/` holds these: separate articles that happen to share a title."""
    vault = FakeVault({"99 topics/lonely 2.md": BASE})
    reaped, findings = reap(vault)
    assert reaped == 0
    assert [f.verdict for f in findings] == [janitor.NO_BASE]


def test_a_torn_read_never_causes_a_delete() -> None:
    """A missing chunk must not read as 'empty', which would read as 'identical'."""
    vault = FakeVault({"99 topics/delta.md": BASE, "99 topics/delta 2.md": BASE})
    vault.torn = {"99 topics/delta 2.md"}
    reaped, findings = reap(vault)
    assert reaped == 0
    assert [f.verdict for f in findings] == [janitor.UNREADABLE]


def test_dry_run_deletes_nothing() -> None:
    vault = FakeVault({"99 topics/delta.md": BASE, "99 topics/delta 2.md": BASE})
    reaped, findings = reap(vault, dry_run=True)
    assert reaped == 0
    assert vault.deleted == []
    assert [f.verdict for f in findings] == [janitor.IDENTICAL]


def test_ordinary_notes_are_untouched() -> None:
    vault = FakeVault({f"99 topics/{n}.md": BASE for n in ("delta", "eu", "ibm")})
    reaped, findings = reap(vault)
    assert (reaped, findings) == (0, [])
    assert vault.deleted == []
