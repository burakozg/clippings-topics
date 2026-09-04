# Clippings → topics

Reads the web clippings in an Obsidian vault's `10 raw/`, works out what each one
is about, and writes that into the topic pages in `99 topics/` — so a clipping
about Anthropic and a podcast episode about Anthropic end up on the same page
instead of sitting in the same vault with nothing between them.

It is the **fourth** application writing to this vault, after `taster`,
`podcast-digest` and `security-digest`. The rules for doing that without eating
another writer's work are in `~/.claude/skills/obsidian-vault-writer`, and this
follows them.

```sh
./deploy                    # build, ship, run it on the NAS
python -m clippings_topics --dry-run     # everything except the writes
python -m clippings_topics --calibrate   # the min_mentions table for this corpus
python -m clippings_topics --only ollama # one topic, for a first run
python -m clippings_topics --reap-only   # just clear duplicated notes
```

## What it writes, and what it never touches

| | |
|---|---|
| Reads | `10 raw/**` — never modified |
| Writes | `99 topics/<slug>.md`, inside `<!-- begin:clippings -->` only |
| Frontmatter | `clip_count`, `clip_first_seen`, `clip_last_seen` |
| Creates | a topic note once `CLIP_MIN_MENTIONS` clippings mention it |

**The clippings themselves are never written to.** Navigation still works both
ways: the topic note links to the clipping, so Obsidian's backlinks panel on the
clipping shows the topic without anything being written there. The Web Clipper
also rewrites those files on a re-clip, so anything added would be at risk anyway.

Everything outside our markers belongs to whoever wrote it. On a real note that
means three writers coexisting:

```yaml
---
tags: [podcast-entity, cybersecurity]   # the reader's — never overwritten
title: "Anthropic"
security_mentions: 19                   # security-digest's
podcasts_mentions: 97                   # podcast-digest's
clip_count: 9                           # ours
---
```

Unprefixed keys (`type`, `title`, `tags`) are **create-only**. Appending our own
`tags: [topic]` under the reader's `tags: [topic, ai]` would silently drop `ai` —
YAML takes the last duplicate key and reports nothing.

## How it decides what changed

One request per run gets the whole picture:

```
GET /tastings/_all_docs?startkey="10 raw/"&endkey="10 raw0"&include_docs=true
```

`include_docs` is a correctness requirement, not an optimisation. **LiveSync does
not tombstone a deleted note** — it keeps a live CouchDB document with
`deleted: true` in the *body*, so `_all_docs` lists deleted notes exactly like
present ones. On this vault that is 141 rows for 109 real notes. Trust the row
list and you publish links to 32 notes that exist on no device.

Each row carries `rev`, so change detection is a diff against a cache of
`doc_id → (rev, content_hash, topics)`:

| in listing | in cache | |
|---|---|---|
| rev differs | yes | **edited** — re-read, re-hash |
| — | no | **new** — read and extract |
| gone / deleted | yes | **dropped** from the mapping |
| rev same | yes | untouched — no read, no LLM |

Two stages deliberately: a changed `rev` costs a read, but only a changed
**content hash** costs an LLM call, because LiveSync rewrites entries for its own
reasons.

Our region is **rebuilt whole every run, never appended to**. An edited clipping
can *remove* a topic, and only a full rebuild makes its line disappear from the
topic it no longer mentions. A rename is a delete plus a create in LiveSync, which
the same rebuild handles for free.

## Duplicated notes, and why they are not ours

The vault is replicated **twice over**: iCloud syncs the folder while LiveSync
syncs the same notes through CouchDB. When a server-side writer *creates* a note,
a LiveSync client goes to write the file, finds the path already occupied by the
copy the other channel delivered, and Obsidian's create-if-exists appends `" 2"` —
which the client then uploads as a fresh document. 54 notes had acquired one by
2026-08-26, accumulating since 2026-07-27.

The signature is unambiguous in CouchDB. Both documents sit at **rev 1** — it can
only happen at creation, never on update — and they are chunked differently:

| | chunks | id scheme |
|---|---|---|
| `99 topics/delta.md` | 1 | `h:t51e43f70…` — a server-side writer |
| `99 topics/delta 2.md` | 2 | `h:25imzxac3eyaw` — the LiveSync client |

No writer can prevent this: the copy is made client-side, after the write has
already succeeded correctly. So `janitor.py` cleans up after it at the start of
every run, and **the rule is byte-identity, not the name**. `10 raw/` legitimately
holds `" 1"` and `" 2"` files — the Web Clipper disambiguating different articles
that share a title, two of them linked — so a copy is reaped only when it is
byte-for-byte its base. Anything that differs is reported and left for a human,
because two files disagreeing is not a question this program can answer.

Deletion goes through the vault, not the disk: the copy exists on every device,
and only the document the clients replicate takes it off all of them. It is
LiveSync's own soft delete (`deleted: true` in the body, children kept) — a real
CouchDB `DELETE` leaves clients holding a file the server cannot describe, and
they put it straight back.

## Links are path-qualified, always

```markdown
- 2026-07-15 · [[10 raw/Claude/The Complete Claude Code Setup for 2026 Every Skill|The Complete Claude Code Setup for 2026: Every Skill…]]
```

Three basenames in `10 raw/` are duplicated (Obsidian's ` 1.md` disambiguation),
so a bare `[[Some Title]]` resolves to whichever Obsidian indexed first. Every
link is qualified rather than only the ambiguous ones — one rule beats a rule
plus an exception. The alias is the clipping's real `title:`, which the filename
often cannot carry (no colons in filenames).

## `min_mentions`, measured

Calibrated on this corpus rather than inherited. `podcast-digest` runs at 8, but
that is over thousands of episodes; 109 clippings is two orders of magnitude
smaller.

| threshold | topic notes |
|---|---|
| 1 | 182 |
| 2 | 52 |
| **3** | **30** ← default |
| 4 | 24 |
| 5 | 20 |
| 8 | 12 |

The collapse from 182 to 52 between 1 and 2 is the one-off mentions falling away.
An **existing** topic note is enriched at any count: the threshold exists to stop
this creating near-empty pages, not to withhold a link from a page already there.

Re-measure with `--calibrate` whenever the corpus grows a lot.

## The model

`google/gemini-2.5-flash-lite` via OpenRouter, chosen by measurement:

- `qwen/qwen3.7-flash` — what the sibling projects use — is hard rate-limited
  upstream and 429s every request. The first full run took 18 minutes of retries
  and finished partial.
- `deepseek/deepseek-chat-v3.1` answers fast but returns genres
  ("Cybersecurity", "Aviation") where this needs named things.
- Gemini Flash Lite answers in ~1s with real entities. Full corpus: ~2 minutes.

The vault's existing topic **titles** are given to the model as the preferred
vocabulary, so it reaches for a name that already exists rather than inventing a
near-spelling. Feeding it filename *slugs* instead taught it to answer in slugs —
every topic came back lowercased, including ones the vault spells "Anthropic".

## Deployment

A compose project on the NAS, deployed over ssh — see `homelab/README.md` for the
contract. It listens on nothing but still needs a qnet address: the vault is
CouchDB on another macvlan address, and the NAS host cannot route to its own
macvlan children, so a bridge-only container could not reach it. Its MAC is
pinned per network, like every other project here.

Runs on start and then weekly, deliberately after `podcast-digest`'s Friday vault
rebuild.

State is the extraction cache at `/data/cache.json`, bind-mounted. Losing it costs
a full re-extraction in money, never in correctness — unchanged input produces
byte-identical notes either way, which is what `--dry-run` twice will show you.

## Tests

`tests/test_notes.py` is the one that matters: it asserts another writer's region
and the reader's frontmatter survive a merge. Everything else is cosmetic by
comparison — a merge bug does not raise, it quietly eats part of somebody's vault
and looks like Obsidian losing notes.

`tests/test_janitor.py` is the other one, for the same reason from the other
direction: it asserts the reaper never deletes two files that disagree, and never
acts on a torn read (a missing chunk reads as empty, and empty would compare
equal to nothing).

```sh
python -m pytest tests/ -q
```
