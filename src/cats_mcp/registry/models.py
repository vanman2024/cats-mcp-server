"""The declarative tool registry model.

One `ToolSpec` per CATS operation. Everything downstream is generated from it:
the registered FastMCP tool and its JSON schema, the authorization checks, the
tool counts, and the endpoint-coverage documentation.

The audit found five different tool counts across README, server logs, tests and
design docs, all hand-maintained and all but one wrong. Making the count a
function of this registry is the fix.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Sentinel for "this parameter is required".
REQUIRED: Any = inspect.Parameter.empty


class ParamLocation(str, Enum):
    """Where a parameter goes in the outgoing HTTP request.

    This is what removes 186 hand-written `make_request` call sites: given the
    location of each parameter, one executor can build every request.
    """

    PATH = "path"
    QUERY = "query"
    BODY = "body"
    #: Consumed locally to shape the response; never sent to CATS.
    SHAPING = "shaping"


class Transform(str, Enum):
    """A value transform applied on the way out to CATS.

    These are not conveniences - each encodes a real CATS API v3 requirement.
    `WRAP_ID` in particular was a bug fix (commit ea735c8): CATS expects tag
    references as `[{"id": 1}]`, not `[1]`, and sending the bare list fails.
    """

    NONE = "none"
    #: [1, 2] -> [{"id": 1}, {"id": 2}]
    WRAP_ID = "wrap_id"
    #: [1, 2] -> [{"candidate_id": 1}, ...]
    WRAP_CANDIDATE_ID = "wrap_candidate_id"
    #: [1, 2] -> [{"job_id": 1}, ...]
    WRAP_JOB_ID = "wrap_job_id"
    #: "123" -> 123. CATS rejects string ids on some POST bodies.
    TO_INT = "to_int"
    #: Merge a caller-supplied dict into the request body at the top level.
    SPREAD = "spread"


def apply_transform(transform: Transform, value: Any) -> Any:
    if transform is Transform.NONE or value is None:
        return value
    if transform is Transform.WRAP_ID:
        return [{"id": v} for v in value]
    if transform is Transform.WRAP_CANDIDATE_ID:
        return [{"candidate_id": v} for v in value]
    if transform is Transform.WRAP_JOB_ID:
        return [{"job_id": v} for v in value]
    if transform is Transform.TO_INT:
        return int(value)
    return value


class Safety(str, Enum):
    """What a tool can do. Drives both metadata and access control."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    BULK = "bulk"
    ADMIN = "admin"

    @property
    def is_mutation(self) -> bool:
        return self is not Safety.READ

    @property
    def required_scope(self) -> str:
        return f"cats:{self.value}"


class ResponseStrategy(str, Enum):
    """How the CATS response is shaped before reaching the model.

    RAW      pass through unchanged. Only for genuinely small responses.
    SUMMARY  compact list shaping: selected fields, counts, pagination hints.
    DETAIL   single record, trimmed of nested collections unless requested.
    BINARY   a file, delivered as MCP content the model can actually read.
    """

    RAW = "raw"
    SUMMARY = "summary"
    DETAIL = "detail"
    #: The endpoint serves a file. Returned as MCP content the model can read -
    #: a resume as a document, a thumbnail as an image - rather than as JSON.
    BINARY = "binary"


@dataclass(frozen=True)
class Param:
    """One tool parameter, and where it belongs in the HTTP request."""

    name: str
    annotation: Any
    description: str
    location: ParamLocation
    default: Any = REQUIRED
    #: Name to send upstream when it differs from the tool-facing name.
    wire_name: str | None = None
    #: Value transform applied before sending. See `Transform`.
    transform: Transform = Transform.NONE

    @property
    def required(self) -> bool:
        return self.default is REQUIRED

    @property
    def outbound_name(self) -> str:
        return self.wire_name or self.name


class Presence(str, Enum):
    """What a verification read should find after the mutation."""

    PRESENT = "present"
    ABSENT = "absent"


@dataclass(frozen=True)
class Verification:
    """How to confirm a mutation actually landed.

    A 2xx from CATS says the request was accepted, not that the record now says
    what you intended. For most writes that distinction is academic. For a Do Not
    Contact list it is not: believing someone was added when they were not means
    contacting a person who asked you to stop, and nothing in the response would
    have told you.

    So the write is followed by a read, and the result reports `verified` rather
    than leaving the caller to assume. Verification never changes what the write
    did; a failed check is reported, not raised, because the mutation may well
    have succeeded and re-running it on that assumption is worse.

    * `endpoint` - what to read back, in the same `{placeholder}` form as a spec
      endpoint. Filled from the call's path arguments.
    * `collection_key` - HAL key holding the rows, when the read is a collection.
    * `identity_field` - the field on each row that identifies the record, e.g.
      `candidate_id` on a saved-list membership row. Note this is deliberately
      not `id`: a membership row's `id` is the row, not the person.
    * `expect_from` - the call argument holding the values that should now be
      present or absent.
    * `presence` - whether those values should be found or gone.
    """

    endpoint: str
    identity_field: str
    expect_from: str
    collection_key: str | None = None
    presence: Presence = Presence.PRESENT


@dataclass(frozen=True)
class ToolSpec:
    """A complete, declarative description of one CATS operation."""

    name: str
    resource: str
    operation: str
    method: str
    endpoint: str
    description: str
    safety: Safety
    tags: frozenset[str] = frozenset()
    params: tuple[Param, ...] = ()
    response: ResponseStrategy = ResponseStrategy.RAW
    #: Collection key in the HAL `_embedded` object, for SUMMARY responses.
    collection_key: str | None = None
    toolset: str = "misc"
    #: Fixed values CATS requires in the body that the caller never supplies,
    #: e.g. `{"type": "job"}` when creating a job list.
    constant_body: tuple[tuple[str, Any], ...] = ()
    deprecated: bool = False
    replaced_by: str | None = None
    #: Extra scopes beyond the one implied by `safety`.
    extra_scopes: frozenset[str] = frozenset()
    #: How to confirm the mutation landed. Reads never carry one.
    verification: Verification | None = None

    # --- derived ------------------------------------------------------------

    @property
    def required_scopes(self) -> frozenset[str]:
        return frozenset({self.safety.required_scope}) | self.extra_scopes

    @property
    def all_tags(self) -> frozenset[str]:
        """Declared tags plus the ones implied by resource and safety.

        Consistent, generated tags matter because BM25 indexes them and because
        visibility policies filter on them. Hand-tagging 186 tools would drift.
        """
        implied = {"ats", self.resource, self.safety.value}
        if self.safety.is_mutation:
            implied.add("write")
        else:
            implied.add("read")
        return frozenset(self.tags) | implied

    def params_at(self, location: ParamLocation) -> tuple[Param, ...]:
        return tuple(p for p in self.params if p.location is location)

    def validate(self) -> list[str]:
        """Structural problems with this spec. Empty means valid."""
        problems: list[str] = []

        if self.method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            problems.append(f"{self.name}: unsupported method {self.method!r}")

        # Every `{placeholder}` in the endpoint needs a PATH param to fill it,
        # and every PATH param needs a placeholder to fill.
        placeholders = set(_placeholders(self.endpoint))
        path_params = {p.outbound_name for p in self.params_at(ParamLocation.PATH)}
        for missing in sorted(placeholders - path_params):
            problems.append(
                f"{self.name}: endpoint needs {{{missing}}} but no PATH param supplies it"
            )
        for unused in sorted(path_params - placeholders):
            problems.append(
                f"{self.name}: PATH param {unused!r} matches no placeholder in {self.endpoint}"
            )

        if self.method.upper() == "GET" and self.params_at(ParamLocation.BODY):
            problems.append(f"{self.name}: GET requests cannot carry a body")

        if self.response is ResponseStrategy.SUMMARY and not self.collection_key:
            problems.append(f"{self.name}: SUMMARY response requires a collection_key")

        if self.deprecated and not self.replaced_by:
            problems.append(f"{self.name}: deprecated tools must name a replacement")

        seen: set[str] = set()
        for p in self.params:
            if p.name in seen:
                problems.append(f"{self.name}: duplicate parameter {p.name!r}")
            seen.add(p.name)

        return problems


def _placeholders(endpoint: str) -> list[str]:
    out: list[str] = []
    depth = 0
    current: list[str] = []
    for char in endpoint:
        if char == "{":
            depth += 1
            current = []
        elif char == "}":
            if depth:
                out.append("".join(current))
            depth -= 1
        elif depth:
            current.append(char)
    return out


@dataclass
class Registry:
    """The full catalog. The single source of truth for what this server exposes."""

    specs: list[ToolSpec] = field(default_factory=list)

    def add(self, spec: ToolSpec) -> ToolSpec:
        self.specs.append(spec)
        return spec

    def extend(self, specs: list[ToolSpec]) -> None:
        self.specs.extend(specs)

    def __len__(self) -> int:
        return len(self.specs)

    def __iter__(self):
        return iter(self.specs)

    def by_name(self, name: str) -> ToolSpec | None:
        return next((s for s in self.specs if s.name == name), None)

    def toolsets(self) -> dict[str, list[ToolSpec]]:
        grouped: dict[str, list[ToolSpec]] = {}
        for spec in self.specs:
            grouped.setdefault(spec.toolset, []).append(spec)
        return grouped

    def counts(self) -> dict[str, int]:
        """Authoritative per-toolset counts. Docs and logs read from here."""
        return {name: len(specs) for name, specs in sorted(self.toolsets().items())}

    def select(self, toolsets: set[str] | None) -> list[ToolSpec]:
        if not toolsets or "all" in toolsets:
            return list(self.specs)
        return [s for s in self.specs if s.toolset in toolsets]

    def validate(self) -> list[str]:
        problems: list[str] = []
        seen: dict[str, ToolSpec] = {}
        for spec in self.specs:
            if spec.name in seen:
                problems.append(f"duplicate tool name {spec.name!r}")
            seen[spec.name] = spec
            problems.extend(spec.validate())
        return problems
