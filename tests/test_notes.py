"""The tests that matter: another writer's content survives our run.

Everything else in this app is cosmetic by comparison. A merge bug here does not
raise — it silently eats a section of somebody's vault, and the damage looks like
Obsidian losing notes rather than like a bug in this program.

The fixture is shaped like a real note from the vault (``99 topics/crowdstrike.md``):
human prose at the top, then podcast-digest's owned region, with frontmatter
carrying both unprefixed keys and another writer's prefixed ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clippings_topics import notes  # noqa: E402

EXISTING = """\
---
tags: [podcast-entity, cybersecurity]
type: topic
title: "CrowdStrike"
podcasts_mentions: 26
podcasts_shows: 8
---

# CrowdStrike

My own view: they ship faster than they explain.

<!-- begin:podcast-digest -->
## From podcasts

- 2026-08-12 · **Cybercrime Magazine Podcast** — Hackers Breach Polish Power Plant `8/10`
<!-- end:podcast-digest -->
"""

OURS = """\
---
type: topic
title: "CrowdStrike"
tags: [topic]
clip_count: 2
---

# CrowdStrike

<!-- begin:clippings -->
## From clippings

- 2026-07-28 · [[10 raw/homelab/some clipping|Some clipping]]
<!-- end:clippings -->
"""


def merged() -> str:
    return notes.merge_owned_section(EXISTING, OURS)


def test_other_writers_region_is_byte_identical() -> None:
    """podcast-digest's section must come through untouched."""
    theirs = EXISTING[
        EXISTING.index("<!-- begin:podcast-digest -->") : EXISTING.index(
            "<!-- end:podcast-digest -->"
        )
        + len("<!-- end:podcast-digest -->")
    ]
    assert theirs in merged()


def test_human_prose_survives() -> None:
    assert "My own view: they ship faster than they explain." in merged()


def test_their_frontmatter_keys_survive() -> None:
    """The bug the skill records: a second ``tags:`` silently drops the reader's.

    PyYAML would return only the last one, with no error. So ``tags`` must keep
    the reader's value and must appear exactly once.
    """
    out = merged()
    assert "tags: [podcast-entity, cybersecurity]" in out
    assert "tags: [topic]" not in out
    assert out.count("tags:") == 1
    # another writer's prefixed keys are not ours to touch
    assert "podcasts_mentions: 26" in out
    assert "podcasts_shows: 8" in out


def test_our_keys_are_written() -> None:
    assert "clip_count: 2" in merged()


def test_our_region_is_added_once() -> None:
    out = merged()
    assert out.count(notes.begin_marker()) == 1
    assert out.count(notes.end_marker()) == 1
    assert "[[10 raw/homelab/some clipping|Some clipping]]" in out


def test_rerun_is_idempotent() -> None:
    """Merging our own output again must change nothing.

    A vault replicating over iCloud *and* LiveSync re-syncs every note we touch,
    so 'unchanged input produces byte-identical output' is a correctness
    property, not a nicety.
    """
    once = merged()
    assert notes.merge_owned_section(once, OURS) == once


def test_our_region_is_replaced_not_appended() -> None:
    """An edited clipping can REMOVE a topic, so the region is rebuilt whole."""
    once = merged()
    fewer = OURS.replace(
        "- 2026-07-28 · [[10 raw/homelab/some clipping|Some clipping]]\n", ""
    ).replace("clip_count: 2", "clip_count: 0")
    twice = notes.merge_owned_section(once, fewer)
    assert "[[10 raw/homelab/some clipping|Some clipping]]" not in twice
    assert twice.count(notes.begin_marker()) == 1
    # and the other writer is still fine
    assert "Hackers Breach Polish Power Plant" in twice


def test_creates_the_note_when_absent() -> None:
    assert notes.merge_owned_section(None, OURS) == OURS
    assert notes.merge_owned_section("", OURS) == OURS


def test_appends_when_note_exists_without_our_marker() -> None:
    """A topic note podcast-digest made before we existed: adopt, don't clobber."""
    out = notes.merge_owned_section(EXISTING, OURS)
    assert out.index("<!-- begin:podcast-digest -->") < out.index(notes.begin_marker())
    assert "# CrowdStrike" in out


DUPLICATED = """\
---
tags: [podcast-entity, cybersecurity]
type: topic
title: "Anthropic"
security_mentions: 19
security_digests: 2
podcasts_mentions: 97
security_mentions: 19
security_digests: 2
---

# Anthropic

<!-- begin:podcast-digest -->
- x
<!-- end:podcast-digest -->
"""


def test_exact_duplicate_keys_are_healed() -> None:
    """Duplicate keys are invalid YAML — Obsidian shows raw text, not properties.

    21 notes in the real vault acquired a second copy of another writer's block.
    A merge must repair that rather than preserve it faithfully.
    """
    out = notes.merge_owned_section(DUPLICATED, OURS)
    front, _ = notes.split_frontmatter(out)
    keys = [line.split(":", 1)[0].strip() for line in front]
    assert [k for k in keys if keys.count(k) > 1] == []
    # and the surviving values are still the other writers'
    assert "security_mentions: 19" in front
    assert "podcasts_mentions: 97" in front


def test_conflicting_duplicates_are_left_alone() -> None:
    """Same key, DIFFERENT values is a real disagreement — not ours to decide."""
    conflicting = DUPLICATED.replace(
        "security_mentions: 19\nsecurity_digests: 2\n---",
        "security_mentions: 42\nsecurity_digests: 2\n---",
    )
    out = notes.merge_owned_section(conflicting, OURS)
    front, _ = notes.split_frontmatter(out)
    assert "security_mentions: 19" in front
    assert "security_mentions: 42" in front  # both kept, for a human to resolve
