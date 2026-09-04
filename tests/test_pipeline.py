"""Parsing, aggregation and rendering — the parts that decide what gets written."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clippings_topics import clippings as clip  # noqa: E402
from clippings_topics import topics as top  # noqa: E402
from clippings_topics.vault import Entry  # noqa: E402

CLIPPING = """\
---
title: "Your homelab deserves a weekly report, not a roaming agent"
source: "https://medium.com/lets-code-future/your-homelab-deserves-115636a7daff"
author:
  - "[[Marius Ionescu]]"
published: 2026-07-21
created: 2026-07-28
description: "More"
tags:
  - "homelab"
---
Last month I noticed my NAS was at 71% disk usage.
"""


def entry(path: str = "10 raw/Homelab/Your homelab deserves a weekly report.md") -> Entry:
    return Entry(
        doc_id=path.lower(), path=path, rev="1-abc", deleted=False, children=("h:t1",), mtime=0
    )


def test_frontmatter_is_parsed_without_yaml() -> None:
    c = clip.parse(entry(), CLIPPING)
    assert c.title == "Your homelab deserves a weekly report, not a roaming agent"
    assert c.source.startswith("https://medium.com/")
    assert "Last month I noticed" in c.body
    # the `author:` list items must not be mistaken for keys
    assert "Marius" not in c.title


def test_published_beats_created() -> None:
    """A topic note reads as a timeline of the subject, not of the reader's browsing."""
    assert clip.parse(entry(), CLIPPING).date == "2026-07-21"


def test_falls_back_to_filename_without_frontmatter() -> None:
    c = clip.parse(entry(), "no frontmatter here")
    assert c.title == "Your homelab deserves a weekly report"
    assert c.date == ""


def test_link_is_path_qualified() -> None:
    """Three basenames in `10 raw/` are duplicated; a bare [[title]] is ambiguous."""
    c = clip.parse(entry(), CLIPPING)
    assert c.link.startswith("[[10 raw/Homelab/Your homelab deserves a weekly report|")
    assert c.link.endswith("]]")
    # the alias carries the real title, which the filename cannot (no colons)
    assert "not a roaming agent]]" in c.link


def test_content_hash_changes_with_the_text() -> None:
    a = clip.parse(entry(), CLIPPING)
    b = clip.parse(entry(), CLIPPING.replace("71%", "98%"))
    assert a.content_hash != b.content_hash


def test_aggregate_collapses_spellings() -> None:
    c = clip.parse(entry(), CLIPPING)
    topics = top.aggregate(
        {c.doc_id: ["CrowdStrike", "crowdstrike", "The Anthropic, Inc."]}, {c.doc_id: c}
    )
    assert set(topics) == {"crowdstrike", "anthropic"}
    assert topics["crowdstrike"].name == "CrowdStrike"  # commonest/longest surface wins
    assert topics["crowdstrike"].slug == "crowdstrike"


def test_aggregate_ignores_a_clipping_that_is_gone() -> None:
    """Extracted last run, deleted since: it must not still contribute topics."""
    assert top.aggregate({"10 raw/gone.md": ["Anthropic"]}, {}) == {}


def test_a_clipping_counts_once_per_topic() -> None:
    c = clip.parse(entry(), CLIPPING)
    topics = top.aggregate({c.doc_id: ["Ollama", "ollama", "OLLAMA"]}, {c.doc_id: c})
    assert len(topics["ollama"].clippings) == 1


def test_render_is_stable_and_marked() -> None:
    c = clip.parse(entry(), CLIPPING)
    topics = top.aggregate({c.doc_id: ["Ollama"]}, {c.doc_id: c})
    once = top.render(topics["ollama"])
    assert top.render(topics["ollama"]) == once  # byte-identical, or the vault re-syncs
    assert "<!-- begin:clippings -->" in once
    assert "clip_count: 1" in once
    assert 'title: "Ollama"' in once
    assert "clip_first_seen: 2026-07-21" in once


def test_render_orders_newest_first() -> None:
    old = clip.parse(entry("10 raw/a/old.md"), CLIPPING.replace("2026-07-21", "2020-01-01"))
    new = clip.parse(entry("10 raw/a/new.md"), CLIPPING)
    topics = top.aggregate({old.doc_id: ["Ollama"], new.doc_id: ["Ollama"]},
                           {old.doc_id: old, new.doc_id: new})
    # Scoped to our region: the frontmatter carries clip_first_seen, which is
    # the OLDEST date and sits above the list by construction.
    note = top.render(topics["ollama"])
    body = note[note.index("<!-- begin:clippings -->") :]
    assert body.index("2026-07-21") < body.index("2020-01-01")


def test_distribution_is_monotonic() -> None:
    c = clip.parse(entry(), CLIPPING)
    topics = top.aggregate({c.doc_id: ["Ollama", "n8n"]}, {c.doc_id: c})
    table = top.distribution(topics)
    counts = [n for _, n in table]
    assert counts == sorted(counts, reverse=True)
