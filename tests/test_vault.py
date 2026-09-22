"""``resolve_topic_names`` must agree with podcast-digest, not recompute.

A topic's filename lives in one shared document (``control:topic_names``) so
two apps naming the same entity from two different corpora land on one file
instead of writing `fortinet.md` beside `fortinet-inc.md`. These tests drive
the method against a fake CouchDB rather than a real one — only the two
requests it actually makes (GET then PUT) need to exist.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clippings_topics.vault import (  # noqa: E402
    TOPIC_NAMES_DOC_ID,
    LiveSyncVault,
    VaultConfig,
    VaultUnavailable,
)


class FakeCouch:
    """The one document this method touches, as a tiny in-memory CouchDB."""

    def __init__(self, doc: dict | None = None, *, conflicts_first: int = 0) -> None:
        self.doc = doc
        self.rev = 1
        self.conflicts_left = conflicts_first
        self.puts: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if self.doc is None:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, json={**self.doc, "_rev": f"{self.rev}-x"})
        assert request.method == "PUT"
        body = json.loads(request.content)
        self.puts.append(body)
        if self.conflicts_left > 0:
            self.conflicts_left -= 1
            return httpx.Response(409, json={"error": "conflict"})
        self.rev += 1
        self.doc = {k: v for k, v in body.items() if k != "_rev"}
        return httpx.Response(201, json={"ok": True, "id": TOPIC_NAMES_DOC_ID, "rev": self.rev})


def _vault(couch: FakeCouch) -> LiveSyncVault:
    vault = LiveSyncVault(VaultConfig(couchdb_url="http://fake", db="the_brain"), "pw")
    vault._client = httpx.AsyncClient(  # noqa: SLF001 — swapping in a mock transport
        base_url="http://fake", transport=httpx.MockTransport(couch.handler)
    )
    return vault


def resolve(vault: LiveSyncVault, proposed: dict[str, str]) -> dict[str, str]:
    return asyncio.run(vault.resolve_topic_names(proposed))


def test_a_new_document_is_created_from_the_proposal() -> None:
    couch = FakeCouch(doc=None)
    names = resolve(_vault(couch), {"anthropic": "anthropic"})
    assert names == {"anthropic": "anthropic"}
    assert couch.doc["names"] == {"anthropic": "anthropic"}


def test_an_existing_pin_wins_over_a_conflicting_proposal() -> None:
    """The exact bug: this app's own corpus would propose `fortinet-inc`,
    but podcast-digest already pinned `fortinet` — that pin must win."""
    couch = FakeCouch(doc={"_id": TOPIC_NAMES_DOC_ID, "names": {"fortinet": "fortinet"}})
    names = resolve(_vault(couch), {"fortinet": "fortinet-inc"})
    assert names["fortinet"] == "fortinet"
    assert couch.puts == []  # nothing to add — no write at all


def test_only_the_missing_keys_are_added() -> None:
    couch = FakeCouch(doc={"_id": TOPIC_NAMES_DOC_ID, "names": {"anthropic": "anthropic"}})
    names = resolve(_vault(couch), {"anthropic": "anthropic-labs", "crowdstrike": "crowdstrike"})
    assert names == {"anthropic": "anthropic", "crowdstrike": "crowdstrike"}
    assert couch.puts[-1]["names"] == {"anthropic": "anthropic", "crowdstrike": "crowdstrike"}


def test_a_conflict_is_retried_against_the_fresh_document() -> None:
    couch = FakeCouch(doc={"_id": TOPIC_NAMES_DOC_ID, "names": {}}, conflicts_first=1)
    names = resolve(_vault(couch), {"volt-typhoon": "volt-typhoon"})
    assert names == {"volt-typhoon": "volt-typhoon"}
    assert len(couch.puts) == 2  # the conflicting attempt, then the retry that won


def test_repeated_conflict_raises_rather_than_looping_forever() -> None:
    couch = FakeCouch(doc={"_id": TOPIC_NAMES_DOC_ID, "names": {}}, conflicts_first=99)
    try:
        resolve(_vault(couch), {"x": "x"})
    except VaultUnavailable:
        return
    raise AssertionError("expected VaultUnavailable after repeated conflicts")
