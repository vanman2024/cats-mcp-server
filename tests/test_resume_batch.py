"""Batch resume reading, and the download it must not repeat.

A live review of 21 candidates spent 27 CATS requests because two files were
fetched more than once and nothing retained what had already been read. These
tests hold that line: an attachment id already seen costs no second download,
and an unreadable resume is reported distinctly rather than as an empty one.
"""

from __future__ import annotations

import io
import json
import zipfile

import httpx2
from fastmcp import Client, FastMCP

from cats_mcp.composites import resumes
from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.http.resume_text import ResumeTextCache

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler, cache: ResumeTextCache | None = None):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    mcp = FastMCP("test")
    resumes.register(
        mcp,
        lambda: client,
        StubCredentials(),
        cache or ResumeTextCache(),
        enforce_auth=False,
    )
    return mcp


def collection(key, rows):
    return {"count": len(rows), "total": len(rows), "_embedded": {key: rows}}


def docx_bytes(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>',
        )
    return buffer.getvalue()


def attachments_for(attachment_id: int, filename: str, is_resume: bool = True):
    return collection(
        "attachments",
        [
            {
                "id": attachment_id,
                "filename": filename,
                "is_resume": is_resume,
                "date_created": "2026-06-01T10:00:00+00:00",
            }
        ],
    )


def make_handler(bodies: dict[int, bytes], calls: list[str]):
    """Serve an attachment list per candidate and file bytes per attachment."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        calls.append(path)

        if path.endswith("/attachments"):
            candidate_id = int(path.split("/candidates/")[1].split("/")[0])
            # Attachment id is derived from the candidate so each has its own.
            return httpx2.Response(200, json=attachments_for(900 + candidate_id, "cv.docx"))

        if "/attachments/" in path and path.endswith("/download"):
            attachment_id = int(path.split("/attachments/")[1].split("/")[0])
            return httpx2.Response(
                200,
                content=bodies[attachment_id],
                headers={"Content-Type": "application/octet-stream"},
            )

        return httpx2.Response(404, json={"message": "unexpected"})

    return handler


async def call(mcp, candidate_ids):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_candidate_resumes", {"candidate_ids": candidate_ids}
        )
    payload = result.structured_content
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


# --- The batch itself ---------------------------------------------------------


async def test_many_candidates_are_read_in_one_call():
    calls: list[str] = []
    bodies = {
        901: docx_bytes(["Brad Willows", "Journeyman Heavy Duty Mechanic"]),
        902: docx_bytes(["Brady Anderson", "Millwright, mining"]),
        903: docx_bytes(["Donald Fraser", "Red Seal 2011"]),
    }
    mcp = build(make_handler(bodies, calls))

    payload = await call(mcp, [1, 2, 3])

    assert payload["count"] == 3
    assert payload["extracted"] == 3
    texts = " ".join(row["text"] for row in payload["resumes"])
    assert "Brad Willows" in texts
    assert "Donald Fraser" in texts
    # Three attachment lists plus three downloads, and nothing more.
    assert payload["execution"]["requests_used"] == 6


async def test_docx_is_read_rather_than_reported_unsupported():
    """DOCX was one of the three formats a live review could not read at all."""
    calls: list[str] = []
    bodies = {901: docx_bytes(["Ian Adams", "Heavy equipment operator"])}
    mcp = build(make_handler(bodies, calls))

    payload = await call(mcp, [1])
    row = payload["resumes"][0]

    assert row["outcome"] == "extracted"
    assert "Ian Adams" in row["text"]


# --- The download that must not repeat ----------------------------------------


async def test_a_second_sweep_does_not_download_again():
    """The measured failure: the same file fetched more than once in one review."""
    calls: list[str] = []
    bodies = {901: docx_bytes(["Brad Willows", "Journeyman HD Mechanic"])}
    cache = ResumeTextCache()
    mcp = build(make_handler(bodies, calls), cache)

    first = await call(mcp, [1])
    downloads_after_first = sum(1 for path in calls if path.endswith("/download"))

    second = await call(mcp, [1])
    downloads_after_second = sum(1 for path in calls if path.endswith("/download"))

    assert downloads_after_first == 1
    assert downloads_after_second == 1, "the attachment was downloaded twice"

    assert first["resumes"][0]["from_cache"] is False
    assert second["resumes"][0]["from_cache"] is True
    assert second["cache_hits"] == 1
    assert second["resumes"][0]["text"] == first["resumes"][0]["text"]


async def test_the_attachment_list_is_still_fetched_every_time():
    """Which attachment is the resume changes when someone uploads a newer one.

    Only the bytes behind a chosen attachment id are retained. Caching the
    choice would serve a stale resume after an upload, which is exactly the
    staleness failure observability.py forbids.
    """
    calls: list[str] = []
    bodies = {901: docx_bytes(["Brad Willows", "Journeyman HD Mechanic"])}
    cache = ResumeTextCache()
    mcp = build(make_handler(bodies, calls), cache)

    await call(mcp, [1])
    await call(mcp, [1])

    listings = [path for path in calls if path.endswith("/attachments")]
    assert len(listings) == 2


# --- Unreadable resumes stay visible ------------------------------------------


async def test_a_candidate_with_no_resume_is_reported_not_dropped():
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        return httpx2.Response(200, json=collection("attachments", []))

    payload = await call(build(handler), [1])
    row = payload["resumes"][0]

    assert row["found"] is False
    assert row["outcome"] == "none"
    assert payload["missing"] == 1
    assert payload["extracted"] == 0


async def test_an_unreadable_format_is_counted_separately_from_a_read_one():
    """'unreadable' must not be silently folded into a low extracted count."""
    calls: list[str] = []
    bodies = {
        901: docx_bytes(["Brad Willows", "Journeyman HD Mechanic"]),
        902: b"{\\rtf1 some rtf that this server does not parse}",
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/attachments"):
            candidate_id = int(path.split("/candidates/")[1].split("/")[0])
            name = "cv.docx" if candidate_id == 1 else "notes.rtf"
            return httpx2.Response(200, json=attachments_for(900 + candidate_id, name))
        attachment_id = int(path.split("/attachments/")[1].split("/")[0])
        return httpx2.Response(200, content=bodies[attachment_id])

    payload = await call(build(handler), [1, 2])

    assert payload["extracted"] == 1
    assert payload["unreadable"] == 1
    unreadable = [r for r in payload["resumes"] if r["outcome"] == "unsupported"]
    assert len(unreadable) == 1
    assert "download_attachment" in unreadable[0]["note"]


async def test_one_candidate_failing_does_not_lose_the_others():
    calls: list[str] = []
    bodies = {902: docx_bytes(["Brady Anderson", "Millwright"])}

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/attachments"):
            candidate_id = int(path.split("/candidates/")[1].split("/")[0])
            if candidate_id == 1:
                return httpx2.Response(500, json={"message": "boom"})
            return httpx2.Response(200, json=attachments_for(900 + candidate_id, "cv.docx"))
        attachment_id = int(path.split("/attachments/")[1].split("/")[0])
        return httpx2.Response(200, content=bodies[attachment_id])

    payload = await call(build(handler), [1, 2])

    assert payload["extracted"] == 1
    assert "Brady Anderson" in payload["resumes"][0]["text"]
    assert payload["execution"]["errors"], "the failed candidate should be reported"


# --- Bounds -------------------------------------------------------------------


async def test_duplicate_ids_are_collapsed():
    calls: list[str] = []
    bodies = {901: docx_bytes(["Brad Willows", "Journeyman HD Mechanic"])}
    mcp = build(make_handler(bodies, calls))

    payload = await call(mcp, [1, 1, 1])

    assert payload["requested"] == 1
    assert payload["count"] == 1
    assert sum(1 for path in calls if path.endswith("/download")) == 1
