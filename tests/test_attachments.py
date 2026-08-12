"""Attachments must come back as documents a model can actually read.

The first implementation of this discarded the bytes and returned only a
content type and a byte count - which made every resume unreadable. Its own
note told the caller to "use the dedicated download tool", which was the tool
producing that message. The whole resume pipeline was a dead end: parse_resume
needs file content, and nothing could supply it.

These tests pin the behaviour that fixes it, and the guard that keeps a large
file from swamping a conversation.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.registry.build import MAX_BINARY_BYTES
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.registry.models import ResponseStrategy

#: A minimal but structurally real PDF, so the bytes are not arbitrary noise.
PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(
            api_key="test-key", base_url="https://api.catsone.com/v3", account_label="test"
        )

    def describe(self) -> str:
        return "stub"


def build(handler):
    settings = Settings(api_key="test-key", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    from cats_mcp.server import create_server

    return create_server(settings, credential_provider=StubCredentials(), client=client)


def serve(content: bytes, content_type: str, filename: str | None = None):
    headers = {"Content-Type": content_type}
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    def handler(request):
        return httpx2.Response(200, content=content, headers=headers)

    return handler


# --- the resume path -------------------------------------------------------


async def test_downloading_a_resume_returns_the_document_itself():
    """The point of the whole exercise: the model receives the actual PDF."""
    async with Client(build(serve(PDF_BYTES, "application/pdf", "dana-reid-resume.pdf"))) as c:
        result = await c.call_tool("download_attachment", {"attachment_id": 42})

    assert result.content, "no content returned"
    block = result.content[0]
    # An embedded resource carrying the file, not a JSON summary of it.
    assert block.type in {"resource", "resource_link"}, f"got {block.type}"
    payload = str(block)
    assert "content_length" not in payload, "returned a summary instead of the file"


async def test_the_returned_document_carries_the_real_bytes():
    async with Client(build(serve(PDF_BYTES, "application/pdf", "cv.pdf"))) as c:
        result = await c.call_tool("download_attachment", {"attachment_id": 42})

    import base64

    resource = result.content[0].resource
    blob = getattr(resource, "blob", None)
    assert blob, "embedded resource has no blob"
    assert base64.b64decode(blob) == PDF_BYTES, "bytes did not survive the round trip"


async def test_the_filename_from_cats_is_preserved():
    async with Client(build(serve(PDF_BYTES, "application/pdf", "dana-reid-resume.pdf"))) as c:
        result = await c.call_tool("download_attachment", {"attachment_id": 42})

    assert "dana-reid-resume" in str(result.content[0].resource.uri)


async def test_a_word_resume_is_labelled_docx_not_its_mime_subtype():
    """The raw subtype is 'vnd.openxmlformats-officedocument...', which is useless."""
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    async with Client(build(serve(b"PK\x03\x04fake-docx", docx, "cv.docx"))) as c:
        result = await c.call_tool("download_attachment", {"attachment_id": 7})

    uri = str(result.content[0].resource.uri)
    assert "wordprocessingml" not in uri
    assert "docx" in uri or "cv" in uri


async def test_a_thumbnail_comes_back_as_an_image():
    async with Client(build(serve(PNG_BYTES, "image/png"))) as c:
        result = await c.call_tool("get_candidate_thumbnail", {"candidate_id": 1})

    assert result.content[0].type == "image", f"got {result.content[0].type}"


# --- the guard -------------------------------------------------------------


async def test_an_oversized_file_is_refused_with_its_actual_size():
    """Base64 inflates by a third and this goes straight into a conversation."""
    oversized = b"%PDF-1.4\n" + b"x" * (MAX_BINARY_BYTES + 1)

    async with Client(build(serve(oversized, "application/pdf"))) as c:
        with pytest.raises(Exception) as excinfo:
            await c.call_tool("download_attachment", {"attachment_id": 42})

    message = str(excinfo.value)
    assert "5MB" in message
    assert "MB," in message or "MB " in message, "the error should name the actual size"


async def test_a_file_just_under_the_limit_is_allowed():
    ok = b"%PDF-1.4\n" + b"x" * (MAX_BINARY_BYTES - 1000)
    async with Client(build(serve(ok, "application/pdf"))) as c:
        result = await c.call_tool("download_attachment", {"attachment_id": 42})
    assert result.content


async def test_a_json_error_body_is_not_dressed_up_as_a_file():
    """CATS may answer a download with a JSON envelope; pass it through as-is."""

    def handler(request):
        return httpx2.Response(200, json={"status": "processing"})

    async with Client(build(handler)) as c:
        result = await c.call_tool("download_attachment", {"attachment_id": 42})

    assert "processing" in str(result.data or result.content)


# --- everything else stays on the JSON path --------------------------------


def test_only_file_serving_tools_are_binary():
    """A stray BINARY strategy would base64 a JSON response into the context."""
    binary = {s.name for s in REGISTRY if s.response is ResponseStrategy.BINARY}
    assert binary == {
        "download_attachment",
        "get_candidate_thumbnail",
        "get_company_thumbnail",
        "get_contact_thumbnail",
    }


async def test_attachment_metadata_is_still_json():
    """list/get describe attachments; only download returns one."""

    def handler(request):
        return httpx2.Response(
            200, json={"id": 42, "filename": "cv.pdf", "is_resume": True}
        )

    async with Client(build(handler)) as c:
        result = await c.call_tool("get_attachment", {"attachment_id": 42})

    assert result.data["filename"] == "cv.pdf"


async def test_binary_is_never_requested_by_a_normal_tool():
    """The client only returns raw bytes when a tool explicitly asks."""
    seen = {}

    def handler(request):
        seen["accept"] = request.headers.get("Accept")
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as c:
        await c.call_tool("get_candidate", {"candidate_id": 1})

    assert seen["accept"] == "application/json"


async def test_a_download_accepts_any_content_type():
    """Asking a file endpoint for JSON only is how you get a 406."""
    seen = {}

    def handler(request):
        seen["accept"] = request.headers.get("Accept")
        return httpx2.Response(
            200, content=PDF_BYTES, headers={"Content-Type": "application/pdf"}
        )

    async with Client(build(handler)) as c:
        await c.call_tool("download_attachment", {"attachment_id": 42})

    assert seen["accept"] == "*/*"
