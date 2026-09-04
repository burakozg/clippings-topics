"""Reading the clippings out of ``10 raw/``.

These are Obsidian Web Clipper captures: an article's text under frontmatter the
clipper wrote (``title``, ``source``, ``author``, ``published``, ``created``,
``description``, ``tags``). 108 of the 109 in the vault carry all seven keys.

We never write to them. They are the reader's notes, and the clipper rewrites
them on a re-clip, so anything we added could vanish without warning.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .vault import Entry, LiveSyncVault

#: Parsed line-by-line rather than with a YAML library: a real parser reformats
#: the human's frontmatter as the price of reading it, and we would then have no
#: way to write the note back unchanged. Same rule as `notes.split_frontmatter`.
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

#: `published: 2026-07-21`, `created: 2026-07-28T09:12:00`, or quoted variants.
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class Clipping:
    entry: Entry
    title: str
    date: str  # YYYY-MM-DD, or "" when the clipper recorded neither
    source: str
    body: str

    @property
    def doc_id(self) -> str:
        return self.entry.doc_id

    @property
    def content_hash(self) -> str:
        """What decides whether the LLM has to look at this again.

        Hashing the whole document, frontmatter included: a retitled clipping is
        a different clipping for our purposes, since the title is what we render
        into every topic note that links to it.
        """
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:16]

    @property
    def link(self) -> str:
        """A wikilink that cannot resolve to the wrong note.

        Path-qualified, always. Three basenames in ``10 raw/`` are duplicated
        (Obsidian's ` 1.md` disambiguation), so a bare ``[[Some Title]]`` picks
        whichever Obsidian happened to index first — a graph that quietly lies,
        which is the exact failure the ownership contract exists to prevent.
        Qualifying every link rather than only the ambiguous ones keeps one rule
        instead of a rule plus an exception.
        """
        target = self.entry.path.removesuffix(".md")
        return f"[[{target}|{self.title}]]"


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group(1).split("\n"):
        if line.startswith((" ", "\t", "-")) or ":" not in line:
            continue  # a list item or continuation belonging to the key above
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, text[match.end() :]


def parse(entry: Entry, markdown: str) -> Clipping:
    fields, body = _frontmatter(markdown)

    title = fields.get("title") or entry.path.rsplit("/", 1)[-1].removesuffix(".md")

    # `published` is when the article was written, `created` when it was clipped.
    # Prefer the former: a topic note reads as a timeline of the subject, not of
    # the reader's browsing.
    date = ""
    for key in ("published", "created"):
        if match := _DATE.search(fields.get(key, "")):
            date = match.group(1)
            break

    return Clipping(
        entry=entry,
        title=title.strip(),
        date=date,
        source=fields.get("source", ""),
        body=body,
    )


async def load(vault: LiveSyncVault, prefix: str = "10 raw/") -> list[Clipping]:
    """Every live clipping, oldest doc_id first.

    Soft-deleted notes are already filtered by :meth:`LiveSyncVault.list_prefix`
    — see the reasoning there; it is the difference between 109 real clippings
    and 141 rows.
    """
    out = []
    for entry in await vault.list_prefix(prefix):
        markdown = await vault.read(entry)
        if markdown is None:
            continue  # torn document: some chunk is missing. Skip rather than guess.
        out.append(parse(entry, markdown))
    return out
