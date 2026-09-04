"""Deciding what a clipping is about, and remembering it.

The LLM is the expensive part, so almost all the work here is avoiding it. A
clipping is sent once; after that its topics come from the cache until the
clipping's *text* changes.

The vault's existing topic names are given to the model as the preferred
vocabulary. Without that it invents a new near-spelling every time — "Anthropic
PBC", "anthropic.com" — and while :func:`~.topics.canonical` collapses some of
that, it is deliberately conservative and will not merge what it cannot prove.
Far better to have the model reach for a name that already exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .clippings import Clipping

log = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: Measured on this corpus before choosing. `qwen/qwen3.7-flash` — what the
#: sibling projects use — is hard rate-limited upstream on OpenRouter and 429s
#: every request, which turned the first full run into 18 minutes of retries for
#: a partial result. `deepseek/deepseek-chat-v3.1` answers fast but returns
#: genres ("Cybersecurity", "Aviation") where this job needs named things.
#: Gemini Flash Lite answers in ~1s with real entities.
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"

#: Enough of an article to tell what it is about. Clippings run to 60 kB; the
#: topic is established in the first couple of pages, and sending the rest is
#: paying to have the model summarise a comment section.
_MAX_CHARS = 6000

_PROMPT = """\
You label a saved article with the topics it is genuinely about.

Rules:
- Return 1-6 topics. Fewer is better than padding.
- A topic is a company, product, technology, standard, person or threat actor —
  something that would deserve its own page. Not a genre ("AI", "security") and
  not a verb phrase.
- STRONGLY prefer a name from the existing list below when the article is about
  that thing. Reuse the exact spelling given.
- Only invent a name when nothing in the list fits.
- If the article is about none of them and names nothing specific, return [].

Existing topics:
{known}

Return ONLY a JSON array of strings. No prose, no code fence.
"""


@dataclass
class Cached:
    rev: str
    content_hash: str
    topics: list[str]


class Cache:
    """``doc_id -> (rev, content_hash, topics)``, as a JSON file.

    State, therefore on a bind mount — the container audit's lesson: anything a
    container writes outside a mount dies with the next recreate. Losing it is a
    cost problem, never a correctness one; a cold run re-extracts everything and
    produces byte-identical output.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Cached] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                log.warning("cache.unreadable path=%s — starting cold", path)
                raw = {}
            for doc_id, value in raw.items():
                self._data[doc_id] = Cached(
                    rev=str(value.get("rev", "")),
                    content_hash=str(value.get("content_hash", "")),
                    topics=[str(t) for t in value.get("topics", [])],
                )

    def get(self, doc_id: str) -> Cached | None:
        return self._data.get(doc_id)

    def put(self, doc_id: str, entry: Cached) -> None:
        self._data[doc_id] = entry

    def keep_only(self, doc_ids: set[str]) -> int:
        """Forget clippings that are no longer in the vault.

        A deleted or renamed clipping must not keep contributing topics — a
        rename shows up as the old id vanishing and a new one appearing, so
        without this the old title would linger in topic notes forever.
        """
        stale = set(self._data) - doc_ids
        for doc_id in stale:
            del self._data[doc_id]
        return len(stale)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            doc_id: {"rev": c.rev, "content_hash": c.content_hash, "topics": c.topics}
            # Sorted so the file does not churn on rewrite.
            for doc_id, c in sorted(self._data.items())
        }
        self._path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def _parse(content: str) -> list[str]:
    """The model's reply as a list of names, or [] if it did not comply."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x).strip() for x in parsed if str(x).strip()]


class Extractor:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = OPENROUTER_BASE_URL,
        concurrency: int = 2,
        timeout_s: float = 90.0,
        attempts: int = 4,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )
        self._gate = asyncio.Semaphore(concurrency)
        self._attempts = attempts

    async def topics_for(self, clipping: Clipping, known: list[str]) -> list[str] | None:
        """The clipping's topics, or None if the model never answered.

        The distinction matters for caching: ``[]`` is a real answer — an article
        about nothing nameable — and must be remembered, or a clipping the model
        declines to label is re-sent on every run forever. ``None`` is a failure
        and must not be cached, so the next run retries exactly it.
        """
        prompt = _PROMPT.format(known="\n".join(f"- {name}" for name in known))
        article = f"# {clipping.title}\n\n{clipping.body[:_MAX_CHARS]}"
        body: dict[str, Any] = {
            "model": self._model,
            "temperature": 0,  # same clipping, same topics, run after run
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": article},
            ],
        }

        for attempt in range(1, self._attempts + 1):
            outcome, payload = await self._once(clipping, body)
            if outcome == "ok":
                return _parse(payload)
            if outcome == "fatal" or attempt == self._attempts:
                log.warning("extract.gave_up id=%s after=%d %s", clipping.doc_id, attempt, payload)
                return None
            # Transient: a shared cheap model is rate-limited upstream far more
            # often than it is broken, and the first run of this job hit 429s and
            # read timeouts on 10 of 109 clippings. Backing off and retrying is
            # the difference between a complete corpus and a partial one.
            delay = min(2 ** attempt, 30)
            log.info(
                "extract.retry id=%s attempt=%d in=%ds (%s)",
                clipping.doc_id, attempt, delay, payload,
            )
            await asyncio.sleep(delay)
        return None

    async def _once(self, clipping: Clipping, body: dict[str, Any]) -> tuple[str, str]:
        """``(outcome, detail)`` where outcome is ok | transient | fatal."""
        async with self._gate:
            try:
                response = await self._client.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                # str(ReadTimeout) is empty, which logged as `err=` and told us
                # nothing on the first run. The class name is the useful part.
                return "transient", f"{type(exc).__name__}: {exc}".rstrip(": ")

        if response.status_code == 200:
            try:
                content = response.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError):
                return "fatal", "malformed response"
            return "ok", str(content)

        detail = f"HTTP {response.status_code} {response.text[:120]}"
        # 429 and 5xx pass; 400/401/403 mean the request or the key is wrong and
        # will be just as wrong in eight seconds.
        if response.status_code == 429 or response.status_code >= 500:
            return "transient", detail
        return "fatal", detail

    async def close(self) -> None:
        await self._client.aclose()


async def resolve(
    clippings: list[Clipping],
    cache: Cache,
    extractor: Extractor | None,
    known: list[str],
) -> dict[str, list[str]]:
    """``{doc_id: [topic names]}`` for every clipping, extracting only what changed.

    Two stages, deliberately. ``rev`` is the cheap filter, but a bump does not
    prove the text changed — LiveSync rewrites entries for its own reasons. So a
    changed rev costs a read (already done by the caller) and only a changed
    content hash costs an LLM call.
    """
    out: dict[str, list[str]] = {}
    todo: list[Clipping] = []

    for clipping in clippings:
        cached = cache.get(clipping.doc_id)
        if cached and cached.content_hash == clipping.content_hash:
            out[clipping.doc_id] = cached.topics
        else:
            todo.append(clipping)

    log.info("extract.plan cached=%d to_extract=%d", len(out), len(todo))
    if not todo:
        return out
    if extractor is None:  # --dry-run without a key
        return out

    # Banked as it goes, not at the end. Extraction is the only slow and only
    # costly step — a 20-minute run that is interrupted at minute 19 must not
    # throw away 19 minutes of answers. Batched rather than per-item so the file
    # is not rewritten 109 times.
    batch = 8
    for start in range(0, len(todo), batch):
        chunk = todo[start : start + batch]
        results = await asyncio.gather(*(extractor.topics_for(c, known) for c in chunk))
        for clipping, topics in zip(chunk, results, strict=True):
            if topics is None:  # failed: not cached, so the next run retries it
                out[clipping.doc_id] = []
                continue
            out[clipping.doc_id] = topics
            cache.put(
                clipping.doc_id,
                Cached(
                    rev=clipping.entry.rev,
                    content_hash=clipping.content_hash,
                    topics=topics,
                ),
            )
        cache.save()
        log.info("extract.progress done=%d/%d", min(start + batch, len(todo)), len(todo))
    return out
