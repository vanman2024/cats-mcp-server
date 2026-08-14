"""Turn `ToolSpec` records into registered FastMCP tools.

One executor serves every tool. The previous implementation hand-wrote a
`make_request(...)` call inside all 186 tool bodies, which is how the two
`filter_*` tools ended up with pagination handling that disagreed with the
coverage report, and how `get_me` shipped pointing at an endpoint that 404s.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from cats_mcp.http.correlation import get_logger, set_run_id
from cats_mcp.http.errors import CATSAPIError, to_tool_error
from cats_mcp.registry.models import (
    Param,
    ParamLocation,
    Presence,
    ResponseStrategy,
    Safety,
    ToolSpec,
    Transform,
    apply_transform,
)

logger = get_logger(__name__)

#: MCP-standard behavioural hints, derived from the safety class so they can
#: never drift from it.
_ANNOTATIONS: dict[Safety, dict[str, bool]] = {
    Safety.READ: {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    Safety.WRITE: {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    Safety.DESTRUCTIVE: {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    Safety.BULK: {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    Safety.ADMIN: {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
}


#: Response-shaping parameters, injected into every tool that shapes its output.
#:
#: These belong to the response strategy, not to individual specs. Only 12 of
#: 103 shaped tools declared `fields` and none declared `summary_level`, so the
#: documented way to widen a response did not exist on 91 tools - an agent that
#: needed custom fields from a list had no option but one request per record.
_SHAPING_PARAMS: tuple[Param, ...] = (
    Param(
        name="summary_level",
        annotation=str,
        description=(
            "How much of each record to return. 'compact' (default) gives a few "
            "identifying fields. 'standard' adds custom fields - certifications, "
            "trade qualifications and other account-specific data. 'full' returns "
            "the whole record. Resumes, attachments, activities and pipelines are "
            "never included at any level; each has its own tool."
        ),
        location=ParamLocation.SHAPING,
        default="compact",
    ),
    Param(
        name="fields",
        annotation=str | None,
        description=(
            "Comma-separated field names to return, e.g. "
            "'id,first_name,city,custom_fields'. Overrides summary_level. Use "
            "'all' for the whole record."
        ),
        location=ParamLocation.SHAPING,
        default=None,
    ),
)


def shaping_params_for(spec: ToolSpec) -> tuple[Param, ...]:
    """Shaping parameters a spec should expose but does not declare itself."""
    if spec.response not in (ResponseStrategy.SUMMARY, ResponseStrategy.DETAIL):
        return ()
    declared = {p.name for p in spec.params}
    return tuple(p for p in _SHAPING_PARAMS if p.name not in declared)


#: Pagination, for the same reason: it belongs to the response strategy, and
#: hand-declaring it per spec is how 34 of 66 list tools ended up unable to
#: reach page 2.
#:
#: The cost was not theoretical. `search_candidates` for "mechanic" matches 3,336
#: records and CATS advertises `page=2` in its own `_links.next` - the tool had
#: no way to send it, so 25 of 3,336 were reachable. Likewise
#: `list_candidate_custom_field_definitions`: 41 definitions, 25 reachable, and
#: the field ids an account screens on are in there.
_PAGINATION_PARAMS: tuple[Param, ...] = (
    Param(
        name="per_page",
        annotation=int,
        description=(
            "Number of results per page (default: 25). Raise it to retrieve a "
            "large set in few requests rather than many."
        ),
        location=ParamLocation.QUERY,
        default=25,
    ),
    Param(
        name="page",
        annotation=int,
        description=(
            "Page number, 1-based (default: 1). Pass the `next_page` value from "
            "the previous response, and stop when `has_more` is false."
        ),
        location=ParamLocation.QUERY,
        default=1,
    ),
)


def pagination_params_for(spec: ToolSpec) -> tuple[Param, ...]:
    """Pagination a collection spec should expose but does not declare itself."""
    if spec.response is not ResponseStrategy.SUMMARY:
        return ()
    declared = {p.name for p in spec.params}
    return tuple(p for p in _PAGINATION_PARAMS if p.name not in declared)


def all_params_for(spec: ToolSpec) -> tuple[Param, ...]:
    """Declared parameters plus everything injected from the response strategy.

    Both the generated signature and the request builder read this. Using
    `spec.params` in one and this in the other is how an injected parameter
    would appear in a tool's schema and then be silently dropped on the way
    out.
    """
    return spec.params + pagination_params_for(spec) + shaping_params_for(spec)


def build_signature(spec: ToolSpec) -> tuple[list[inspect.Parameter], dict[str, Any]]:
    """Build the parameter list and annotations for a spec's tool function.

    Required parameters must precede optional ones or `inspect.Signature`
    rejects the result.
    """
    all_params = all_params_for(spec)
    ordered = sorted(all_params, key=lambda p: (not p.required,))
    annotations: dict[str, Any] = {}
    sig_params: list[inspect.Parameter] = []
    # A file-returning tool must not claim to return a dict, or FastMCP derives
    # an output schema requiring structured JSON and rejects the file.
    returns = Any if spec.response is ResponseStrategy.BINARY else dict[str, Any]

    for param in ordered:
        annotated = Annotated[param.annotation, Field(description=param.description)]
        annotations[param.name] = annotated
        sig_params.append(
            inspect.Parameter(
                param.name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=param.default,
                annotation=annotated,
            )
        )

    annotations["return"] = returns
    return sig_params, annotations


def split_arguments(spec: ToolSpec, kwargs: dict[str, Any]) -> tuple[str, dict, dict | None]:
    """Split call arguments into (endpoint, query params, JSON body)."""
    by_name = {p.name: p for p in all_params_for(spec)}

    path_values: dict[str, Any] = {}
    query: dict[str, Any] = {}
    body: dict[str, Any] = dict(spec.constant_body)

    for key, value in kwargs.items():
        param: Param | None = by_name.get(key)
        if param is None:
            continue
        if param.location is ParamLocation.SHAPING:
            # Consumed by the response shaper, never sent upstream.
            continue
        if value is None and not param.required:
            # Omit unset optionals rather than sending nulls. Note this differs
            # from the previous code, which tested truthiness - so passing ""
            # to clear a field silently did nothing.
            continue

        value = apply_transform(param.transform, value)

        if param.location is ParamLocation.PATH:
            path_values[param.outbound_name] = value
        elif param.location is ParamLocation.QUERY:
            query[param.outbound_name] = value
        elif param.transform is Transform.SPREAD and isinstance(value, dict):
            # A caller-supplied dict merged in at the top level, e.g. the
            # arbitrary application payload for a portal submission.
            body.update(value)
        else:
            body[param.outbound_name] = value

    endpoint = spec.endpoint.format(**path_values)
    return endpoint, query, (body or None)


#: Pages to sweep when confirming a mutation against a collection. A stop, so
#: verifying an addition to a very large list cannot itself become the expensive
#: operation the rest of this module works to avoid.
MAX_VERIFY_PAGES = 20
VERIFY_PAGE_SIZE = 100


def _expected_values(kwargs: dict[str, Any], field: str) -> list[str]:
    value = kwargs.get(field)
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


def _rows_from(payload: Any, collection_key: str | None) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    embedded = payload.get("_embedded")
    if isinstance(embedded, dict):
        if collection_key and isinstance(embedded.get(collection_key), list):
            return [r for r in embedded[collection_key] if isinstance(r, dict)]
        for value in embedded.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return [payload]


async def _verified(
    spec: ToolSpec,
    raw: Any,
    kwargs: dict[str, Any],
    client: Any,
    run_id: str,
) -> dict[str, Any]:
    """Run the mutation's verification read and report what it found.

    A failed check is reported, never raised. The mutation may well have
    succeeded, and turning a successful write into an exception invites a caller
    to retry it - which for an addition is harmless and for anything else is not.
    """
    check = spec.verification
    assert check is not None
    expected = set(_expected_values(kwargs, check.expect_from))

    result: dict[str, Any] = {"result": raw, "verified": False}
    if not expected:
        result["verification"] = "nothing to verify: no expected values supplied"
        return result

    path_values = {
        p.outbound_name: kwargs[p.name]
        for p in spec.params_at(ParamLocation.PATH)
        if p.name in kwargs
    }
    try:
        endpoint = check.endpoint.format(**path_values)
    except KeyError as exc:
        logger.warning("tool=%s cannot build verification endpoint: %s", spec.name, exc)
        result["verification"] = f"could not build the verification read: missing {exc}"
        return result

    found: set[str] = set()
    page = 1
    try:
        while page <= MAX_VERIFY_PAGES:
            payload = await client.request(
                "GET",
                endpoint,
                params={"per_page": VERIFY_PAGE_SIZE, "page": page},
                # A verification read must not retry for a minute: the write has
                # already happened and the caller is waiting on the answer.
                max_attempts=1,
            )
            rows = _rows_from(payload, check.collection_key)
            for row in rows:
                value = row.get(check.identity_field)
                if value is not None:
                    found.add(str(value))
            links = payload.get("_links") if isinstance(payload, dict) else None
            if not rows or not (isinstance(links, dict) and "next" in links):
                break
            page += 1
    except CATSAPIError as exc:
        logger.warning("tool=%s verification read failed: %s request=%s", spec.name, exc, run_id)
        result["verification"] = (
            f"the write was accepted but could not be confirmed: {exc}. "
            f"Re-read before assuming it did or did not land."
        )
        return result

    if check.presence is Presence.PRESENT:
        missing = sorted(expected - found)
        result["verified"] = not missing
        result["confirmed"] = sorted(expected & found)
        if missing:
            result["missing"] = missing
            result["verification"] = (
                f"CATS accepted the write but {check.identity_field} "
                f"{missing} are still not present at {endpoint}."
            )
    else:
        remaining = sorted(expected & found)
        result["verified"] = not remaining
        if remaining:
            result["still_present"] = remaining
            result["verification"] = (
                f"CATS accepted the write but {check.identity_field} "
                f"{remaining} are still present at {endpoint}."
            )

    return result


def make_tool_function(
    spec: ToolSpec, client_getter: Callable[[], Any], ui_domain: Any = ""
) -> Callable:
    """Create the async callable FastMCP will register for this spec.

    `ui_domain` is either a fixed base URL or a `UIDomainResolver`, which reads
    the account's own subdomain from `GET /site`. Resolving it per call rather
    than baking one value into the process is what keeps links correct when
    different callers bring different CATS accounts.
    """
    sig_params, annotations = build_signature(spec)
    from cats_mcp.responses.shaping import spec_can_emit_url

    # Most tools can never attach a URL, so they must never pay for looking one up.
    links_possible = spec_can_emit_url(spec)

    async def impl(**kwargs: Any) -> Any:
        run_id = set_run_id()
        endpoint, query, body = split_arguments(spec, kwargs)

        # "Empty" means the caller supplied nothing - constant body fields do
        # not count, or a create tool with a fixed `type` would look populated.
        caller_supplied = set(body or {}) - {k for k, _ in spec.constant_body}
        if spec.safety.is_mutation and spec.params_at(ParamLocation.BODY) and not caller_supplied:
            # An update with no fields silently no-ops upstream and reads as a
            # success, which is worse than an error.
            raise to_tool_error(
                CATSAPIError(
                    f"{spec.name} was called with no fields to change. Supply at least one value.",
                    endpoint=endpoint,
                    correlation_id=run_id,
                )
            )

        client = client_getter()
        wants_bytes = spec.response is ResponseStrategy.BINARY
        try:
            raw = await client.request(
                spec.method,
                endpoint,
                params=query or None,
                json=body,
                raw_bytes=wants_bytes,
            )
        except CATSAPIError as exc:
            logger.warning(
                "tool=%s endpoint=%s failed: %s request=%s",
                spec.name,
                endpoint,
                exc,
                run_id,
            )
            raise to_tool_error(exc) from exc

        if wants_bytes:
            return _as_mcp_file(spec, raw, run_id)

        if spec.verification is not None:
            return await _verified(spec, raw, kwargs, client, run_id)

        from cats_mcp.responses.shaping import shape_response

        base_url = ""
        if links_possible:
            base_url = ui_domain if isinstance(ui_domain, str) else await ui_domain.resolve()

        return shape_response(spec, raw, kwargs, base_url)

    impl.__name__ = spec.name
    impl.__qualname__ = spec.name
    impl.__doc__ = spec.description
    returns = annotations["return"]
    impl.__signature__ = inspect.Signature(sig_params, return_annotation=returns)  # type: ignore[attr-defined]
    impl.__annotations__ = annotations
    return impl


def register_spec(
    mcp: Any,
    spec: ToolSpec,
    client_getter: Callable[[], Any],
    *,
    enforce_auth: bool,
    ui_domain: Any = "",
) -> None:
    """Register one spec as a tool on the given FastMCP server."""
    fn = make_tool_function(spec, client_getter, ui_domain)

    annotations = dict(_ANNOTATIONS[spec.safety])
    # CATS is an external system whose state changes outside this server.
    annotations["openWorldHint"] = True

    kwargs: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "tags": set(spec.all_tags),
        "annotations": annotations,
        "meta": {
            "cats": {
                "method": spec.method.upper(),
                "endpoint": spec.endpoint,
                "resource": spec.resource,
                "operation": spec.operation,
                "safety": spec.safety.value,
                "toolset": spec.toolset,
                "deprecated": spec.deprecated,
            }
        },
    }

    if spec.response is ResponseStrategy.BINARY:
        # The result is an embedded file or image, not structured JSON.
        kwargs["output_schema"] = None

    if enforce_auth:
        # Only applied when an MCP auth provider is configured. Attaching scope
        # checks to an unauthenticated server would deny every call, including
        # legitimate local stdio use where the transport is already private.
        from fastmcp.server.auth import require_scopes

        kwargs["auth"] = require_scopes(*sorted(spec.required_scopes))

    mcp.tool(**kwargs)(fn)


def register_all(
    mcp: Any,
    specs: list[ToolSpec],
    client_getter: Callable[[], Any],
    *,
    enforce_auth: bool,
    ui_domain: Any = "",
) -> int:
    for spec in specs:
        register_spec(mcp, spec, client_getter, enforce_auth=enforce_auth, ui_domain=ui_domain)
    return len(specs)


#: Base64 inflates a payload by roughly a third, and the result goes straight
#: into a model's context. A 5MB cap comfortably covers resumes and portfolios
#: while refusing anything that would swamp a conversation.
MAX_BINARY_BYTES = 5 * 1024 * 1024


def _as_mcp_file(spec: ToolSpec, payload: Any, run_id: str) -> Any:
    """Turn a `BinaryPayload` into MCP content the model can actually read.

    The previous implementation discarded the bytes and returned only a size and
    a content type, which made every attachment unreadable - the note it
    returned even pointed at the download tool that produced it.
    """
    from fastmcp.utilities.types import File, Image

    from cats_mcp.http.client import BinaryPayload

    if not isinstance(payload, BinaryPayload):
        # The endpoint answered with JSON after all - an error envelope, most
        # likely. Pass it through rather than pretending it is a file.
        return payload

    size = len(payload.content)
    if size > MAX_BINARY_BYTES:
        raise to_tool_error(
            CATSAPIError(
                f"{spec.name} returned {size / 1_048_576:.1f}MB, over the "
                f"{MAX_BINARY_BYTES / 1_048_576:.0f}MB limit for inline content. "
                f"Retrieve this file outside the conversation.",
                endpoint=spec.endpoint,
                correlation_id=run_id,
            )
        )

    content_type = (payload.content_type or "").split(";")[0].strip().lower()
    subtype = content_type.rsplit("/", 1)[-1] or "octet-stream"

    if content_type.startswith("image/"):
        return Image(data=payload.content, format=subtype)

    return File(
        data=payload.content,
        format=_FORMAT_BY_CONTENT_TYPE.get(content_type, subtype),
        name=payload.filename or spec.resource,
    )


#: Content types CATS serves for attachments, mapped to the format hint the
#: model needs. Falling back to the MIME subtype alone yields useless values
#: like "vnd.openxmlformats-officedocument.wordprocessingml.document".
_FORMAT_BY_CONTENT_TYPE = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/rtf": "rtf",
    "text/plain": "txt",
    "text/html": "html",
    "application/octet-stream": "bin",
}
