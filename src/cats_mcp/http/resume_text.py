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

Memory, or a store
------------------
In-process memory is the default and is always the first tier. It is also gone
on restart and private to one replica, so a shared store can be configured
behind it.

That choice is not only about speed. Resume text is personal data - names,
addresses, phone numbers, employment history. Holding it in memory means it
evaporates; persisting it means it lives outside CATS in something that needs
its own access control, retention policy and deletion path. A candidate erased
from CATS would still be in the store, and nothing here would know to remove
them. Hence the TTL: not for correctness, which needs none, but to bound how
long that is true.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Any

from cats_mcp.documents import Extraction, Outcome
from cats_mcp.http.correlation import get_logger

logger = get_logger(__name__)

#: In-memory entries retained before the least recently used is dropped. Each
#: holds at most MAX_TEXT_CHARS of text, so the ceiling is a few tens of
#: megabytes in the worst case and far less in practice.
MAX_ENTRIES = 512

#: Namespace in the shared store. Keeps this data identifiable and separately
#: expirable from anything else the same store might hold.
COLLECTION = "cats-resume-text"


class ResumeCacheUnavailableError(RuntimeError):
    """A cache URL was configured but its backend is not installed."""


def account_key(api_key: str) -> str:
    """A stable per-account cache key that is not the secret itself.

    Same construction as http/custom_fields.py and http/site.py: keyed on the
    credential so two tenants under request-scoped credentials can never collide
    into reading each other's documents out of one cache. This matters more with
    a shared store than with process memory, because the blast radius of getting
    it wrong is every replica rather than one.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def build_store(url: str) -> Any | None:
    """Build the shared cache backend for `url`, or None when unset.

    Fails at startup rather than on first use. A misconfigured cache that only
    surfaced when someone ran a sweep would look like a tool bug, and one that
    silently fell back to memory would look like the cache simply never helping.
    """
    url = (url or "").strip()
    if not url:
        return None

    scheme = url.split("://", 1)[0].lower()

    if scheme in ("redis", "rediss", "valkey"):
        try:
            from key_value.aio.stores.redis import RedisStore
        except ImportError as exc:
            raise ResumeCacheUnavailableError(
                f"CATS_RESUME_CACHE_URL={url!r} needs the redis backend, which is "
                f"not installed. Install it with:\n"
                f"    uv pip install 'cats-mcp-server[redis-cache]'\n"
                f"Or unset CATS_RESUME_CACHE_URL to keep resume text in process "
                f"memory only."
            ) from exc
        # Valkey speaks the Redis protocol; the client only knows redis://.
        return RedisStore(url=url.replace("valkey://", "redis://", 1))

    if scheme == "disk":
        try:
            from key_value.aio.stores.disk import DiskStore
        except ImportError as exc:
            raise ResumeCacheUnavailableError(
                f"CATS_RESUME_CACHE_URL={url!r} needs the disk backend, which is "
                f"not installed. Install it with:\n"
                f"    uv pip install 'cats-mcp-server[disk-cache]'\n"
                f"Note that a disk cache only survives restart on a persistent "
                f"volume - on ephemeral container storage it buys nothing over "
                f"process memory."
            ) from exc
        return DiskStore(directory=url.split("://", 1)[1] or ".")

    raise ResumeCacheUnavailableError(
        f"CATS_RESUME_CACHE_URL={url!r} is not a supported cache URL. Use "
        f"redis://, rediss://, valkey://, or disk:///path - or leave it unset to "
        f"keep resume text in process memory."
    )


class ResumeTextCache:
    """Extracted text keyed by (account, attachment id).

    Two tiers. Process memory is always first and always present. A shared store,
    when configured, sits behind it so text survives restart and is visible to
    every replica.

    The in-memory tier has no TTL: the key names immutable bytes, so the value
    can never go stale and eviction is purely about memory. The store tier does
    carry one, for retention rather than correctness - see the module docstring.
    """

    def __init__(
        self,
        store: Any | None = None,
        *,
        max_entries: int = MAX_ENTRIES,
        ttl_seconds: int | None = None,
    ) -> None:
        self._entries: OrderedDict[str, Extraction] = OrderedDict()
        self._max = max_entries
        self._store = store
        self._ttl = ttl_seconds
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(account: str, attachment_id: object) -> str:
        return f"{account}:{attachment_id}"

    def describe(self) -> str:
        backing = type(self._store).__name__ if self._store else "memory only"
        return f"resume text cache ({backing}, {len(self._entries)} in memory)"

    async def get(self, account: str, attachment_id: object) -> Extraction | None:
        key = self._key(account, attachment_id)

        found = self._entries.get(key)
        if found is not None:
            self._entries.move_to_end(key)
            self.hits += 1
            return found

        if self._store is not None:
            payload = await self._read_store(key)
            if payload is not None:
                found = _from_payload(payload)
                if found is not None:
                    # Promote into memory so the next read in this sweep is local.
                    self._remember(key, found)
                    self.hits += 1
                    return found

        self.misses += 1
        return None

    async def put(self, account: str, attachment_id: object, extraction: Extraction) -> None:
        key = self._key(account, attachment_id)
        self._remember(key, extraction)

        if self._store is not None:
            try:
                await self._store.put(
                    key, _to_payload(extraction), collection=COLLECTION, ttl=self._ttl
                )
            except Exception as exc:  # noqa: BLE001 - a cache write must not fail a call
                logger.warning("resume text cache write failed for %s: %s", key, exc)

    async def _read_store(self, key: str) -> dict[str, Any] | None:
        try:
            return await self._store.get(key, collection=COLLECTION)
        except Exception as exc:  # noqa: BLE001 - degrade to a miss, never fail the call
            logger.warning("resume text cache read failed for %s: %s", key, exc)
            return None

    def _remember(self, key: str, extraction: Extraction) -> None:
        self._entries[key] = extraction
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            evicted, _ = self._entries.popitem(last=False)
            logger.debug("resume text cache evicted %s", evicted)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        """A cache always exists; its emptiness is not its truthiness.

        Without this, `__len__` makes a cold cache falsy, so the natural
        `cache or ResumeTextCache()` silently swaps a configured cache - store
        and all - for a fresh empty one, and the only symptom is that caching
        never appears to work. Caught by the restart test doing exactly that.
        """
        return True


def _to_payload(extraction: Extraction) -> dict[str, Any]:
    return {
        "outcome": extraction.outcome.value,
        "text": extraction.text,
        "parser": extraction.parser,
        "note": extraction.note,
    }


def _from_payload(payload: dict[str, Any]) -> Extraction | None:
    """Rebuild an Extraction, tolerating anything the store hands back.

    A store shared across deployments can hold an entry written by an older
    version. An unreadable entry is a cache miss, never an error.
    """
    try:
        return Extraction(
            outcome=Outcome(payload["outcome"]),
            text=payload.get("text", "") or "",
            parser=payload.get("parser", "") or "",
            note=payload.get("note", "") or "",
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.debug("discarding unreadable resume cache entry: %s", exc)
        return None


__all__ = [
    "ResumeTextCache",
    "ResumeCacheUnavailableError",
    "account_key",
    "build_store",
    "COLLECTION",
    "MAX_ENTRIES",
]
