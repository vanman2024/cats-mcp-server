"""Typed contracts shared by the composite reads.

Issue #17: the composites returned `dict[str, Any]`, so FastMCP had nothing to
generate an output schema from and nothing to validate against. A key could be
renamed, or quietly stop being emitted, and every test would still pass because
every test asserted on a dictionary the same code produced.

What lives here is only what is genuinely common. The rows each composite
returns are its own business - a timeline event and a job match have nothing in
common and should not be forced through one model to look tidy. What they do
share is how they report on *themselves*: how much budget they spent, what they
could not do, and whether the answer is complete.

That distinction is the reason for `ExecutionFacts`. It is not domain data and
it is not decoration - a caller that cannot see `truncated` or `next_cursor`
cannot tell "that is everything" from "that is where I stopped", which is the
difference between a correct answer and a confidently wrong one.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RateLimit(BaseModel):
    """The live CATS budget, as the client last saw it.

    Both fields are nullable because CATS only reports them on responses that
    carry the headers; a call served entirely from the reference-data cache
    leaves them unset. Null means "not observed", never "unlimited".
    """

    limit: int | None = Field(default=None, description="Requests allowed in the window.")
    remaining: int | None = Field(default=None, description="Requests left in the window.")


class MatchEvidence(BaseModel):
    """Why one record is in a result.

    Every composite that narrows a set has to answer "why is this row here?",
    and the answer has to be data rather than prose - a caller filtering on it
    should not be parsing a sentence. Keeping it typed is also what stops
    evidence drifting into a verdict: there is nowhere in this model to put a
    score, and tests/test_boundary.py fails the build if one appears.
    """

    field: str = Field(description="The field that matched, e.g. 'title' or 'email'.")
    value: str | None = Field(
        default=None, description="The stored value on the record that matched."
    )
    matched: str | None = Field(default=None, description="The caller's term that matched it.")
    source: str | None = Field(
        default=None, description="Where the value was read from, when it is not obvious."
    )
    mode: str | None = Field(
        default=None, description="How it was compared: exact, contains or prefix."
    )


class ExecutionFacts(BaseModel):
    """What a composite spent, and what it could not finish.

    Deliberately separate from the rows. A caller reasons about these to decide
    whether to ask again, raise a budget, or trust the count - which is why
    they stay in structured output rather than moving to ToolResult.meta, where
    they would read as telemetry rather than as part of the answer.
    """

    requests_used: int = Field(description="CATS requests this call spent.")
    rate_limit: RateLimit = Field(description="Live budget after the call.")
    truncated: bool = Field(
        default=False,
        description="True when a limit stopped the work early. Check next_cursor.",
    )
    next_cursor: str | None = Field(
        default=None,
        description="Pass back to continue the same sweep. Null means it finished.",
    )
    errors: dict[str, str] = Field(
        default_factory=dict,
        description="Per-stage failures. Present here rather than raised, so a "
        "partial answer is still returned instead of discarding the requests "
        "already spent.",
    )
