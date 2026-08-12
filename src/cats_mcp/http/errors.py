"""Error normalization for CATS API failures.

Two audiences, two needs:

* The **model** gets a short, actionable message. Raw CATS error bodies can be
  large and are frequently HTML on 5xx; dumping one into a tool result burns
  context and tells the model nothing it can act on.
* The **logs** get the detail, correlated by request id.

Nothing raised from here may contain a credential.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

# Bodies are truncated before they ever reach the model. CATS 5xx responses in
# particular can be full HTML error pages.
_MAX_DETAIL_CHARS = 500


class CATSAPIError(RuntimeError):
    """A CATS API call failed.

    Retained under its original name: the previous implementation raised this
    and tests/consumers may catch it.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        endpoint: str | None = None,
        correlation_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint
        self.correlation_id = correlation_id
        self.retryable = retryable


def _truncate(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _MAX_DETAIL_CHARS:
        return text
    return f"{text[:_MAX_DETAIL_CHARS]}... [truncated]"


#: Status codes mapped to guidance the model can actually act on. The generic
#: "API HTTP error 404: <body>" the previous implementation produced told an
#: agent nothing about whether to retry, fix its arguments, or give up.
_GUIDANCE: dict[int, str] = {
    400: "The request was rejected as malformed. Check argument types and required fields.",
    401: "CATS rejected the credential. The API key is missing, wrong, or revoked. "
    "This is a server configuration problem, not something to retry.",
    403: "The CATS account lacks permission for this operation.",
    404: "No such record. The id may be wrong, or the record may have been deleted.",
    409: "The request conflicts with the current state of the record.",
    422: "CATS understood the request but rejected the values. Check field formats.",
    429: "Rate limited by CATS.",
}


def normalize_http_error(
    status_code: int,
    body: str,
    *,
    endpoint: str,
    correlation_id: str,
) -> CATSAPIError:
    """Turn a CATS HTTP failure into a bounded, actionable error."""
    guidance = _GUIDANCE.get(status_code)
    if guidance is None:
        if 500 <= status_code < 600:
            guidance = "CATS returned a server error. Retrying later may succeed."
        else:
            guidance = "The CATS API returned an unexpected status."

    detail = _truncate(body) if body else ""
    message = f"CATS {status_code} on {endpoint}: {guidance}"
    if detail:
        message = f"{message} Response: {detail}"

    return CATSAPIError(
        message,
        status_code=status_code,
        endpoint=endpoint,
        correlation_id=correlation_id,
        # 429 is handled by the retry layer; if it reaches here retries are spent.
        retryable=500 <= status_code < 600,
    )


def to_tool_error(exc: CATSAPIError) -> ToolError:
    """Convert to the exception FastMCP surfaces to the client.

    Raising a bare exception from a tool risks leaking internals into the
    transcript; `ToolError` is the sanctioned user-facing channel.
    """
    suffix = f" [request {exc.correlation_id}]" if exc.correlation_id else ""
    return ToolError(f"{exc}{suffix}")
