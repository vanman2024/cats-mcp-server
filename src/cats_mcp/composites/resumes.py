"""Reading a batch of resumes as text, in one call.

The problem this replaces
-------------------------
`find_candidate_resume` returns one candidate's resume as the document itself,
for the model to read. That is right for one person and does not scale: a
shortlist of twenty costs twenty tool calls, twenty model round trips, and
twenty whole documents held in context at once. `discovery/profiles.py` already
notes that model turns dominate wall-clock; this is that cost at its worst.

A live review of 21 candidates measured the shape of it: 27 CATS requests, six
of them re-downloads of two files already fetched moments earlier, and three
candidates whose resumes could not be read at all because the extractor in front
of the tool did not handle DOCX or HTML.

This tool does the fan-out and the extraction server-side. One call, one turn,
every format the server can read, and text instead of documents.

What it does not decide
-----------------------
Nothing here ranks, scores or shortlists. It returns what each resume says and
whether it could be read. Judging the candidate is the caller's, and
`tests/test_boundary.py` fails the build if a verdict appears in the output.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, Field

from cats_mcp.composites.models import ExecutionFacts, RateLimit
from cats_mcp.composites.reads import (
    MAX_BATCH,
    _dedupe,
    _embedded_rows,
    _gather_by_id,
    _looks_like_a_resume,
    _parse_iso,
    _project,
)
from cats_mcp.documents import Outcome, extract_text
from cats_mcp.http.correlation import get_logger, set_run_id
from cats_mcp.http.resume_text import ResumeTextCache, account_key
from cats_mcp.responses.shaping import SUMMARY_FIELDS

logger = get_logger(__name__)

#: Matches the ceiling download_attachment enforces. A resume is not a 5MB file;
#: something that large is a scanned booklet, and parsing it would cost more
#: memory than the answer is worth.
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024


class ResumeRow(BaseModel):
    """One candidate's resume, or a precise account of why there isn't one."""

    candidate_id: str = Field(description="The candidate this row is about.")
    found: bool = Field(description="Whether an attachment that looks like a resume exists.")
    outcome: str = Field(
        description=(
            "extracted, empty, unsupported, failed, or none. 'empty' on a PDF "
            "means a scan with no text layer - the file exists and has to be read "
            "as an image. 'none' means no resume-like attachment at all."
        )
    )
    text: str = Field(default="", description="The resume as text. Empty unless outcome=extracted.")
    attachment: dict[str, Any] = Field(
        default_factory=dict, description="The attachment this text came from."
    )
    selection_method: str = Field(
        default="",
        description=(
            "How the attachment was chosen: the is_resume flag, or a filename "
            "heuristic, which is a guess and says so."
        ),
    )
    from_cache: bool = Field(
        default=False,
        description=(
            "True when text was already held for this attachment id, costing no download."
        ),
    )
    note: str = Field(
        default="",
        description="Why reading failed, or what was truncated. Empty on a clean extraction.",
    )


class ResumeBatchResult(BaseModel):
    """Resumes for a batch of candidates, with what the call cost."""

    resumes: list[ResumeRow]
    count: int = Field(description="Rows returned.")
    requested: int = Field(description="Distinct candidate ids asked for.")
    extracted: int = Field(description="How many produced readable text.")
    unreadable: int = Field(
        description=(
            "How many exist but could not be read - scans, unsupported formats, "
            "corrupt files. Fetch these with download_attachment to read directly."
        )
    )
    missing: int = Field(description="How many have no resume-like attachment at all.")
    cache_hits: int = Field(description="Documents served without a download.")
    execution: ExecutionFacts


def register(
    mcp: Any,
    client_getter: Callable[[], Any],
    credentials: Any,
    cache: ResumeTextCache,
    *,
    enforce_auth: bool,
) -> int:
    """Register the batch resume composite. Returns how many tools were added."""
    tool_kwargs: dict[str, Any] = {}
    if enforce_auth:
        from fastmcp.server.auth import require_scopes

        tool_kwargs["auth"] = require_scopes("cats:read")

    @mcp.tool(
        name="get_candidate_resumes",
        output_schema=ResumeBatchResult.model_json_schema(),
        description=(
            "Read a batch of candidates' resumes as text in one call. Use this "
            "instead of calling find_candidate_resume per person: it fetches "
            f"concurrently, extracts the text server-side, and handles up to {MAX_BATCH} "
            "candidates at once. PDF, DOCX, HTML and plain text are read; anything "
            "else is reported as unsupported rather than returned empty.\n\n"
            "Check `outcome` on every row before concluding anything about a "
            "candidate. outcome='empty' on a PDF means a scanned document with no "
            "text layer - the resume exists and is unread, which is not the same as "
            "a thin resume. Those rows, and 'unsupported' and 'failed' ones, can be "
            "fetched with download_attachment and read directly.\n\n"
            "Text for a given attachment is retained after the first read, so "
            "re-running a sweep costs no download for documents already seen."
        ),
        tags={"ats", "candidate", "attachment", "resume", "read", "batch"},
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        **tool_kwargs,
    )
    async def get_candidate_resumes(
        candidate_ids: Annotated[
            list[int | str],
            Field(description=f"Candidates whose resumes to read. Up to {MAX_BATCH}."),
        ],
    ) -> Any:
        set_run_id()
        client = client_getter()

        unique = _dedupe(list(candidate_ids))
        truncated = len(unique) > MAX_BATCH
        ids = unique[:MAX_BATCH]

        # The account key partitions the text cache. Resolution failing is not
        # this call's problem - it degrades to an unshared partition rather than
        # risking one account reading another's documents out of cache.
        try:
            credential = await credentials.resolve(None)
            account = account_key(credential.api_key)
        except Exception:  # noqa: BLE001 - see above
            account = "unresolved"

        requests_before = {"n": 0}

        async def one(candidate_id: int | str) -> dict[str, Any]:
            payload = await client.request(
                "GET", f"/candidates/{candidate_id}/attachments", params={"per_page": 100}
            )
            requests_before["n"] += 1
            attachments = _embedded_rows(payload)

            # The choice of which attachment is the resume is answered live every
            # time, never cached: it changes the moment someone uploads a newer
            # file. Only the bytes behind a chosen attachment id are retained.
            flagged = [a for a in attachments if a.get("is_resume")]
            if flagged:
                pool, method = flagged, "is_resume flag"
            else:
                pool = [a for a in attachments if _looks_like_a_resume(a)]
                method = "filename heuristic - nothing was flagged is_resume, this is a guess"

            if not pool:
                return {
                    "candidate_id": str(candidate_id),
                    "found": False,
                    "outcome": "none",
                    "note": (
                        "No attachment on this candidate looks like a resume."
                        if attachments
                        else "This candidate has no attachments."
                    ),
                }

            epoch = datetime.min.replace(tzinfo=timezone.utc)
            chosen = max(pool, key=lambda a: _parse_iso(a.get("date_created")) or epoch)
            row: dict[str, Any] = {
                "candidate_id": str(candidate_id),
                "found": True,
                "attachment": _project(chosen, SUMMARY_FIELDS["attachment"]),
                "selection_method": method,
            }

            cached = cache.get(account, chosen.get("id"))
            if cached is not None:
                row.update(
                    outcome=cached.outcome.value,
                    text=cached.text,
                    note=cached.note,
                    from_cache=True,
                )
                return row

            binary = await client.request(
                "GET", f"/attachments/{chosen['id']}/download", raw_bytes=True
            )
            requests_before["n"] += 1

            data = getattr(binary, "content", b"") or b""
            if len(data) > MAX_DOCUMENT_BYTES:
                row.update(
                    outcome=Outcome.UNSUPPORTED.value,
                    note=(
                        f"The file is {len(data)} bytes, over the "
                        f"{MAX_DOCUMENT_BYTES} byte limit. Fetch it with "
                        f"download_attachment if it is genuinely needed."
                    ),
                )
                return row

            filename = getattr(binary, "filename", None) or chosen.get("filename")
            extraction = extract_text(data, filename)
            cache.put(account, chosen["id"], extraction)

            row.update(
                outcome=extraction.outcome.value,
                text=extraction.text,
                note=extraction.note,
                from_cache=False,
            )
            return row

        hits_before = cache.hits
        results, errors = await _gather_by_id(ids, one)
        cache_hits = cache.hits - hits_before

        rows = [results[str(i)] for i in ids if str(i) in results]
        extracted = sum(1 for r in rows if r.get("outcome") == Outcome.EXTRACTED.value)
        missing = sum(1 for r in rows if r.get("outcome") == "none")
        unreadable = len(rows) - extracted - missing

        return ResumeBatchResult(
            resumes=[ResumeRow(**r) for r in rows],
            count=len(rows),
            requested=len(unique),
            extracted=extracted,
            unreadable=unreadable,
            missing=missing,
            cache_hits=cache_hits,
            execution=ExecutionFacts(
                requests_used=requests_before["n"],
                rate_limit=RateLimit(**(client.rate_limit.snapshot() or {})),
                truncated=truncated,
                errors=errors,
            ),
        ).model_dump()

    return 1


__all__ = ["register", "ResumeBatchResult", "ResumeRow", "MAX_DOCUMENT_BYTES"]
