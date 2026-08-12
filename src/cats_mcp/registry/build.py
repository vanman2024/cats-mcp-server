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


def build_signature(spec: ToolSpec) -> tuple[list[inspect.Parameter], dict[str, Any]]:
    """Build the parameter list and annotations for a spec's tool function.

    Required parameters must precede optional ones or `inspect.Signature`
    rejects the result.
    """
    ordered = sorted(spec.params, key=lambda p: (not p.required,))
    annotations: dict[str, Any] = {}
    sig_params: list[inspect.Parameter] = []

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

    annotations["return"] = dict[str, Any]
    return sig_params, annotations


def split_arguments(spec: ToolSpec, kwargs: dict[str, Any]) -> tuple[str, dict, dict | None]:
    """Split call arguments into (endpoint, query params, JSON body)."""
    by_name = {p.name: p for p in spec.params}

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


def make_tool_function(spec: ToolSpec, client_getter: Callable[[], Any]) -> Callable:
    """Create the async callable FastMCP will register for this spec."""
    sig_params, annotations = build_signature(spec)

    async def impl(**kwargs: Any) -> dict[str, Any]:
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
        try:
            raw = await client.request(spec.method, endpoint, params=query or None, json=body)
        except CATSAPIError as exc:
            logger.warning(
                "tool=%s endpoint=%s failed: %s request=%s",
                spec.name,
                endpoint,
                exc,
                run_id,
            )
            raise to_tool_error(exc) from exc

        from cats_mcp.responses.shaping import shape_response

        return shape_response(spec, raw, kwargs)

    impl.__name__ = spec.name
    impl.__qualname__ = spec.name
    impl.__doc__ = spec.description
    impl.__signature__ = inspect.Signature(sig_params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    impl.__annotations__ = annotations
    return impl


def register_spec(
    mcp: Any,
    spec: ToolSpec,
    client_getter: Callable[[], Any],
    *,
    enforce_auth: bool,
) -> None:
    """Register one spec as a tool on the given FastMCP server."""
    fn = make_tool_function(spec, client_getter)

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
) -> int:
    for spec in specs:
        register_spec(mcp, spec, client_getter, enforce_auth=enforce_auth)
    return len(specs)
