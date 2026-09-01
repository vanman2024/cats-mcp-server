"""Extracted resume text, cached per account by attachment id.

Why this cache is allowed when record caching is not
----------------------------------------------------
`observability.py` forbids caching record data, and it is right to: a recruiter
screens a list, someone lands on Do Not Contact, the screen runs again and the
cache serves the stale answer. That reasoning turns on records *changing*.

An attachment's bytes do not change. CATS attachment ids are immutable - a
revised resume is uploaded as a new attachment with a new id, it does not
rewrite an existing one. So text keyed on attachment id can never go stale: the
same key always describes the same bytes.

What is deliberately NOT cached here is *which* attachment is a candidate's
resume. That question is answered live on every call, because the answer changes
the moment someone uploads a newer file. Cache the bytes, never the choice.

The measured cost of having no cache: a review of 21 candidates spent 27 CATS
requests, six of them re-downloading two files that had already been fetched
moments earlier, because a retry has nothing to retry against.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from cats_mcp.documents import Extraction
from cats_mcp.http.correlation import get_logger

logger = get_logger(__name__)

#: Entries retained before the least recently used is dropped. Each holds at
#: most MAX_TEXT_CHARS of text, so the ceiling is a few tens of megabytes in the
#: worst case and far less in practice. Bounded because a long-lived server
#: sweeping a large account would otherwise grow without limit.
MAX_ENTRIES = 512


def account_key(api_key: str) -> str:
    """A stable per-account cache key that is not the secret itself.

    Same construction as http/custom_fields.py and http/site.py: keyed on the
    credential so two tenants under request-scoped credentials can never collide
    into reading each other's documents out of one cache.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


class ResumeTextCache:
    """LRU of extracted text, keyed by (account, attachment id).

    No TTL. A TTL would express a belief that the value can go stale, and it
    cannot - the key names immutable bytes. Eviction is purely about memory.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: OrderedDict[str, Extraction] = OrderedDict()
        self._max = max_entries
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(account: str, attachment_id: object) -> str:
        return f"{account}:{attachment_id}"

    def get(self, account: str, attachment_id: object) -> Extraction | None:
        key = self._key(account, attachment_id)
        found = self._entries.get(key)
        if found is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return found

    def put(self, account: str, attachment_id: object, extraction: Extraction) -> None:
        key = self._key(account, attachment_id)
        self._entries[key] = extraction
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            evicted, _ = self._entries.popitem(last=False)
            logger.debug("resume text cache evicted %s", evicted)

    def __len__(self) -> int:
        return len(self._entries)


__all__ = ["ResumeTextCache", "account_key", "MAX_ENTRIES"]
