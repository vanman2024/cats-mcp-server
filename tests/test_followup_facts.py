"""Follow-up facts, and the properties that keep them facts.

Issue #12 item 3's worked example is "candidates whose interview stage is more
than 24 hours old with no subsequent activity". Every noun in that sentence
belongs to the person asking: which stage counts as an interview, how many
hours is too many, what counts as activity. A tool that supplied any of them
would be shipping one customer's recruiting policy to every other account on
the same adapter.

So the failure modes worth guarding are not "does it return rows" but:

  * the threshold, the stages and the activity types all come from the caller,
    and there is no default number of hours to fall back on
  * the arithmetic is right, and reproducible - the reference instant is an
    argument, not the wall clock
  * the boundary is pinned in one direction and documented in the same one
  * a record that could not be assessed is reported as unknown, never as one
    that failed the threshold
  * every returned row carries enough to redo the comparison by hand
  * nothing in the output, or in the declared schema, is shaped like a verdict

Each test below fails loudly if one of those regresses.

Assertions read `result.structured_content` - the dict the tool actually put on
the wire. `result.data` is a dataclass FastMCP synthesises from the declared
output schema, so it is not subscriptable and a recursive dict walk over it
would pass by walking nothing at all.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from fastmcp import Client, FastMCP

# The two enforcement lists live in the boundary suite; importing them keeps
# this file honest if either grows a new word or key, rather than letting a
# hand-copied duplicate drift out of date.
from test_boundary import BANNED_VERDICT_KEYS, TOOL_VOCAB, _find_banned_keys

from cats_mcp.composites import followup
from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    """A server carrying find_followup_facts alone - it is not wired into
    create_server yet."""
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    mcp = FastMCP("test")
    followup.register(mcp, lambda: client, enforce_auth=False)
    return mcp


def collection(rows, *, key="rows", has_next=False):
    payload = {"count": len(rows), "total": len(rows), "_embedded": {key: rows}}
    if has_next:
        payload["_links"] = {"next": {"href": "?page=2"}}
    return payload


# --- one small account, shaped like the issue's example ----------------------
#
# Two people sitting in the same stage since the same moment. One has had
# nothing logged since; the other was emailed nine hours later. Everything the
# tool is meant to distinguish is that difference and nothing else.

#: The account's own stage ids. Nothing in the module knows what they mean.
INTERVIEW = 3
OFFER = 9

STAGE_ENTERED = "2026-01-05T09:00:00-00:00"
AS_OF = "2026-01-06T12:00:00-00:00"
#: 2026-01-05T09:00 to 2026-01-06T12:00 is 27 hours exactly.
ELAPSED = 27.0

JOB_PIPELINES = [
    {
        "id": 101,
        "candidate_id": 42,
        "job_id": 7,
        "status_id": INTERVIEW,
        "date_modified": STAGE_ENTERED,
    },
    {
        "id": 102,
        "candidate_id": 43,
        "job_id": 7,
        "status_id": INTERVIEW,
        "date_modified": STAGE_ENTERED,
    },
    {
        "id": 103,
        "candidate_id": 44,
        "job_id": 7,
        "status_id": OFFER,
        "date_modified": "2026-01-06T08:00:00-00:00",
    },
]

WORKFLOWS = {
    "_embedded": {
        "workflows": [
            {
                "id": 1,
                "statuses": [
                    {"id": INTERVIEW, "title": "Interview"},
                    {"id": OFFER, "title": "Offer"},
                ],
            }
        ]
    }
}


def account_handler(request, *, calls=None):
    """One handler for the account above."""
    path = request.url.path
    if calls is not None:
        calls.append(path)

    if path.endswith("/pipelines/workflows"):
        return httpx2.Response(200, json=WORKFLOWS)
    if path.endswith("/jobs/7/pipelines"):
        return httpx2.Response(200, json=collection(JOB_PIPELINES, key="pipelines"))
    if path.endswith("/pipelines/101/statuses") or path.endswith("/pipelines/102/statuses"):
        return httpx2.Response(
            200,
            json=collection(
                [{"id": 1, "status_id": INTERVIEW, "date_created": STAGE_ENTERED}],
                key="statuses",
            ),
        )
    if path.endswith("/pipelines/103/statuses"):
        return httpx2.Response(
            200,
            json=collection(
                [{"id": 2, "status_id": OFFER, "date_created": "2026-01-06T08:00:00-00:00"}],
                key="statuses",
            ),
        )
    if path.endswith("/candidates/42/activities"):
        # Before the stage change, so nothing has happened since.
        return httpx2.Response(
            200,
            json=collection(
                [
                    {
                        "id": 1,
                        "type": "Call",
                        "notes": "Left a message for Dana Lee about the Artemis site",
                        "date_created": "2026-01-04T10:00:00-00:00",
                    }
                ],
                key="activities",
            ),
        )
    if path.endswith("/candidates/43/activities"):
        # Nine hours after the stage change.
        return httpx2.Response(
            200,
            json=collection(
                [
                    {
                        "id": 2,
                        "type": "Email",
                        "notes": "Sent Priya Raman the schedule",
                        "date_created": "2026-01-05T18:00:00-00:00",
                    }
                ],
                key="activities",
            ),
        )
    if path.endswith("/activities"):
        return httpx2.Response(200, json=collection([], key="activities"))
    return httpx2.Response(200, json={})


BASE_CALL = {
    "job_ids": [7],
    "pipeline_status_ids": [INTERVIEW],
    "older_than_hours": 24,
    "require_no_later_activity": True,
    "as_of": AS_OF,
}


#: Every top-level field the structured payload must carry, spelled out rather
#: than read back off FollowupResult - a list derived from the model would agree
#: with it by construction and could never catch a field being dropped.
EXPECTED_TOP_LEVEL = frozenset(
    {
        "records",
        "count",
        "total_matched",
        "criteria",
        "pipelines_seen",
        "pipelines_in_scope",
        "candidates_read",
        "evaluated",
        "below_threshold",
        "with_later_activity",
        "unevaluated",
        "execution",
        "note",
    }
)

#: Ceiling on the human-readable line, in bytes. Generous next to the ~60 bytes
#: a real call produces, and still far below any payload it could be a copy of.
CONTENT_BYTE_CEILING = 160


def _resolve_ref(schema: dict, node):
    """Follow a JSON Schema `$ref` into the document's own `$defs`.

    Pydantic hoists every nested model into `$defs`, so `properties.execution`
    is a reference rather than the object itself. A test that asserted on the
    reference would be asserting that a pointer exists, not that the contract
    behind it is the right shape.
    """
    ref = node.get("$ref") if isinstance(node, dict) else None
    if not ref:
        return node
    return schema.get("$defs", {}).get(ref.rsplit("/", 1)[-1], {})


def _schema_property_names(schema) -> set[str]:
    """Every property name a JSON schema declares, at any nesting depth."""
    found: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            found.update(str(key) for key in properties)
        for value in schema.values():
            found |= _schema_property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= _schema_property_names(item)
    return found


# --- the headline: the threshold separates two otherwise identical records ---


async def test_a_record_over_the_threshold_with_nothing_since_is_returned():
    """The reason the tool exists. Pipelines 101 and 102 entered the same stage
    at the same instant; the only difference is that one of them has had an
    email logged since. That difference, and no other, decides the result."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    data = result.structured_content
    assert [row["pipeline_id"] for row in data["records"]] == [101]
    assert data["count"] == 1
    assert data["total_matched"] == 1
    assert data["evaluated"] == 2, "both in-scope pipelines were compared"
    assert data["with_later_activity"] == 1, (
        "the record held back has to be counted, or a caller cannot tell it from "
        "one that was never looked at"
    )
    assert data["below_threshold"] == 0
    assert data["pipelines_seen"] == 3
    assert data["pipelines_in_scope"] == 2, "the offer-stage pipeline was filtered out"


async def test_a_record_with_later_activity_is_not_returned():
    async with Client(build(account_handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    returned = {row["pipeline_id"] for row in result.structured_content["records"]}
    assert 102 not in returned
    assert not [
        row for row in result.structured_content["unevaluated"] if row["pipeline_id"] == 102
    ], "a record that was compared and held back is not an unevaluated one"


async def test_dropping_the_no_later_activity_condition_returns_both():
    """`require_no_later_activity` is a condition the caller applies, not one
    the adapter has an opinion about. Without it, both records come back and
    the facts needed to apply it are still on every row."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool(
            "find_followup_facts", {**BASE_CALL, "require_no_later_activity": False}
        )

    data = result.structured_content
    assert sorted(row["pipeline_id"] for row in data["records"]) == [101, 102]
    assert data["with_later_activity"] == 0
    by_id = {row["pipeline_id"]: row for row in data["records"]}
    assert by_id[102]["activities_after_anchor"] == 1
    assert by_id[101]["activities_after_anchor"] == 0


# --- every row carries the facts the comparison used -------------------------


async def test_every_returned_row_can_be_re_derived_by_hand():
    """Item 6 of the brief: the stage, the timestamp used, the elapsed hours and
    the last activity date. A row a caller cannot check is an oracle, not a
    fact."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    row = result.structured_content["records"][0]
    assert row["candidate_id"] == 42
    assert row["job_id"] == 7
    assert row["status_id"] == INTERVIEW
    assert row["status"] == "Interview", "an account-specific id needs its label"
    assert row["anchor"] == "stage_entered"
    assert row["anchor_type"] == "status_change"
    assert row["anchor_date"] == STAGE_ENTERED
    assert row["anchor_date_field"] == "date_created"
    assert row["elapsed_hours"] == ELAPSED
    assert row["last_activity_date"] == "2026-01-04T10:00:00-00:00"
    assert row["last_activity_type"] == "Call"
    assert row["activities_after_anchor"] == 0
    assert row["activities_undated"] == 0


async def test_the_criteria_echo_everything_the_comparison_needed():
    """The reference instant and both operators come back with the answer. An
    elapsed time whose reference instant is unknown cannot be checked, and
    "older than" is ambiguous at the boundary until something says which way."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    criteria = result.structured_content["criteria"]
    assert criteria["older_than_hours"] == 24
    assert criteria["comparison"] == "elapsed_hours >= older_than_hours"
    assert criteria["later_comparison"] == "activity_date > anchor_date"
    assert criteria["as_of_source"] == "caller"
    assert criteria["as_of"].startswith("2026-01-06T12:00:00")
    assert criteria["anchor"] == "stage_entered"
    assert criteria["require_no_later_activity"] is True
    assert criteria["pipeline_status_ids"] == [INTERVIEW]


# --- the arithmetic ----------------------------------------------------------


@pytest.mark.parametrize(
    "as_of,expected",
    [
        ("2026-01-06T12:00:00-00:00", 27.0),
        ("2026-01-05T09:30:00-00:00", 0.5),
        ("2026-01-08T09:00:00-00:00", 72.0),
        ("2026-01-05T09:00:00.900000-00:00", 0.0003),
    ],
)
async def test_elapsed_hours_is_measured_from_the_instant_the_caller_pinned(as_of, expected):
    """`as_of` is an argument, so the arithmetic is checked against a fixed
    instant rather than against whatever the clock said while the suite ran.

    The last case is the rounding: 0.9 seconds is 0.00025 hours, which is the
    value the comparison is made on as well as the value reported, so the two
    can never disagree.
    """
    async with Client(build(account_handler)) as client:
        result = await client.call_tool(
            "find_followup_facts",
            {**BASE_CALL, "as_of": as_of, "older_than_hours": 0.0001},
        )

    row = next(r for r in result.structured_content["records"] if r["pipeline_id"] == 101)
    assert row["elapsed_hours"] == expected


async def test_nothing_has_elapsed_at_the_anchor_instant_itself():
    """Zero hours is under every positive threshold, so the record is counted
    as compared rather than vanishing from both lists."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool(
            "find_followup_facts",
            {**BASE_CALL, "as_of": STAGE_ENTERED, "older_than_hours": 0.0001},
        )

    data = result.structured_content
    assert data["records"] == []
    assert data["evaluated"] == 2
    assert data["below_threshold"] == 2


async def test_the_reference_instant_defaults_to_the_server_clock_and_says_so():
    """Omitting as_of is allowed, and the result still reports the instant it
    used - an elapsed time measured from a clock reading nobody recorded is not
    reproducible."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool(
            "find_followup_facts",
            {"job_ids": [7], "older_than_hours": 1, "pipeline_status_ids": [INTERVIEW]},
        )

    criteria = result.structured_content["criteria"]
    assert criteria["as_of_source"] == "server clock at call time"
    assert criteria["as_of"], "the instant used has to be reported either way"


async def test_an_unreadable_as_of_is_refused_rather_than_ignored():
    """Falling back to the clock would return elapsed times measured from an
    instant the caller did not choose and cannot see they did not get."""
    async with Client(build(account_handler)) as client:
        with pytest.raises(Exception, match="as_of"):
            await client.call_tool(
                "find_followup_facts", {**BASE_CALL, "as_of": "last tuesday"}
            )


# --- the boundary, pinned ----------------------------------------------------


def single_pipeline_handler(entered):
    """An account with exactly one pipeline, entered at `entered`."""

    def handler(request):
        path = request.url.path
        if path.endswith("/pipelines/workflows"):
            return httpx2.Response(200, json=WORKFLOWS)
        if path.endswith("/jobs/7/pipelines"):
            return httpx2.Response(
                200,
                json=collection(
                    [
                        {
                            "id": 101,
                            "candidate_id": 42,
                            "job_id": 7,
                            "status_id": INTERVIEW,
                            "date_modified": entered,
                        }
                    ],
                    key="pipelines",
                ),
            )
        if path.endswith("/statuses"):
            return httpx2.Response(
                200,
                json=collection(
                    [{"id": 1, "status_id": INTERVIEW, "date_created": entered}],
                    key="statuses",
                ),
            )
        if path.endswith("/activities"):
            return httpx2.Response(200, json=collection([], key="activities"))
        return httpx2.Response(200, json={})

    return handler


async def test_a_record_at_exactly_the_threshold_is_returned():
    """The comparison is `elapsed_hours >= older_than_hours`, inclusive, and it
    is documented that way in the description and echoed in `criteria`. This
    test is what stops the two disagreeing."""
    handler = single_pipeline_handler("2026-01-05T12:00:00-00:00")

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "find_followup_facts",
            {**BASE_CALL, "older_than_hours": 24},
        )

    data = result.structured_content
    assert data["records"][0]["elapsed_hours"] == 24.0
    assert data["total_matched"] == 1
    assert data["below_threshold"] == 0


async def test_a_record_just_under_the_threshold_is_counted_not_returned():
    handler = single_pipeline_handler("2026-01-05T12:00:00-00:00")

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "find_followup_facts", {**BASE_CALL, "older_than_hours": 24.0001}
        )

    data = result.structured_content
    assert data["records"] == []
    assert data["evaluated"] == 1
    assert data["below_threshold"] == 1
    assert data["unevaluated"] == [], "a record that was compared is not an unassessed one"


async def test_an_activity_at_exactly_the_anchor_is_not_later_activity():
    """`activity_date > anchor_date`, strictly. An activity at the anchor
    instant is usually the anchor itself, and counting it as something that
    happened afterwards would hide every record the caller asked about."""

    def handler(request):
        path = request.url.path
        if path.endswith("/activities"):
            return httpx2.Response(
                200,
                json=collection(
                    [{"id": 1, "type": "Note", "date_created": STAGE_ENTERED}],
                    key="activities",
                ),
            )
        return account_handler(request)

    async with Client(build(handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    data = result.structured_content
    assert sorted(row["pipeline_id"] for row in data["records"]) == [101, 102]
    assert all(row["activities_after_anchor"] == 0 for row in data["records"])


async def test_later_activity_types_narrow_what_counts_as_later():
    """Which activity types matter is the caller's vocabulary, not a taxonomy
    this adapter carries. The email on 102 stops counting when the caller says
    only calls do - and `last_activity_date` still shows it."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool(
            "find_followup_facts", {**BASE_CALL, "later_activity_types": ["Call"]}
        )

    data = result.structured_content
    by_id = {row["pipeline_id"]: row for row in data["records"]}
    assert sorted(by_id) == [101, 102]
    assert by_id[102]["activities_after_anchor"] == 0
    assert by_id[102]["last_activity_date"] == "2026-01-05T18:00:00-00:00", (
        "a narrowed type list must not hide the activity that actually happened"
    )


# --- anchoring on an activity instead of a stage -----------------------------


async def test_the_anchor_can_be_an_activity_type_the_caller_names():
    """CATS v3 has no interview resource, so "interviewed" is whatever the
    account calls it. Naming an activity type is one of the two ways to say it,
    and the row reports which activity it measured from."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool(
            "find_followup_facts",
            {
                "job_ids": [7],
                "pipeline_status_ids": [INTERVIEW],
                "older_than_hours": 24,
                "anchor": "activity",
                "anchor_activity_types": ["call"],
                "as_of": AS_OF,
            },
        )

    data = result.structured_content
    assert [row["pipeline_id"] for row in data["records"]] == [101]
    row = data["records"][0]
    assert row["anchor"] == "activity"
    assert row["anchor_type"] == "Call"
    assert row["anchor_date"] == "2026-01-04T10:00:00-00:00"
    assert row["elapsed_hours"] == 50.0

    # Candidate 43 has an Email and no Call, so there is nothing to measure
    # from - reported as unknown, not as a record that failed the threshold.
    unknown = {row["pipeline_id"]: row for row in data["unevaluated"]}
    assert unknown[102]["reason"] == "anchor_missing"


# --- awkward records are reported, never dropped -----------------------------


async def test_an_unreadable_timestamp_is_reported_with_the_raw_value():
    """The one thing a filter must never do is quietly lose a record because
    its date did not parse. It comes back in `unevaluated` with the original
    string, so the underlying record can be corrected."""

    def handler(request):
        path = request.url.path
        if path.endswith("/pipelines/101/statuses"):
            return httpx2.Response(
                200,
                json=collection(
                    [{"id": 1, "status_id": INTERVIEW, "date_created": "05/01/2026 9am"}],
                    key="statuses",
                ),
            )
        return account_handler(request)

    async with Client(build(handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    data = result.structured_content
    assert 101 not in {row["pipeline_id"] for row in data["records"]}

    bad = next(row for row in data["unevaluated"] if row["pipeline_id"] == 101)
    assert bad["reason"] == "anchor_date_unreadable"
    assert bad["date_raw"] == "05/01/2026 9am"
    assert bad["date_field"] == "date_created"
    assert bad["candidate_id"] == 42
    assert bad["detail"], "a reason code alone is not something a caller can act on"
    assert data["evaluated"] == 1, "the unreadable record was never compared"


async def test_a_pipeline_with_no_stage_history_is_unknown_not_a_miss():
    def handler(request):
        if request.url.path.endswith("/statuses"):
            return httpx2.Response(200, json=collection([], key="statuses"))
        return account_handler(request)

    async with Client(build(handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    data = result.structured_content
    assert data["records"] == []
    assert data["evaluated"] == 0
    assert {row["reason"] for row in data["unevaluated"]} == {"anchor_missing"}
    assert data["execution"]["truncated"] is False, (
        "an empty stage history is a fact about the record, not a limit that "
        "stopped the call early"
    )


async def test_a_failed_read_is_reported_rather_than_raised():
    def handler(request):
        if request.url.path.endswith("/candidates/42/activities"):
            return httpx2.Response(404, json={"message": "Not found"})
        return account_handler(request)

    async with Client(build(handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    data = result.structured_content
    assert 101 not in {row["pipeline_id"] for row in data["records"]}
    bad = next(row for row in data["unevaluated"] if row["pipeline_id"] == 101)
    assert bad["reason"] == "read_failed"
    assert data["execution"]["errors"], "the underlying failure has to surface too"


# --- the request budget ------------------------------------------------------


async def test_the_free_stage_filter_keeps_paid_reads_off_out_of_scope_records():
    """`pipeline_status_ids` is applied on rows already fetched. Spending a
    stage read and an activity read on a pipeline that could not have matched
    is the difference between six requests and eight."""
    calls: list[str] = []

    def handler(request):
        return account_handler(request, calls=calls)

    async with Client(build(handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    assert "/v3/pipelines/103/statuses" not in calls
    assert "/v3/candidates/44/activities" not in calls
    assert result.structured_content["execution"]["requests_used"] == 6, (
        "one pool page, one workflow read, two stage reads, two activity reads"
    )
    assert result.structured_content["candidates_read"] == 2


async def test_the_budget_is_respected_and_every_record_it_missed_is_named():
    """A budget stop must be visible per record. An unreached pipeline is not
    one that failed the threshold, and reporting it as one is how a caller ends
    up sure that only one person is waiting."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool(
            "find_followup_facts", {**BASE_CALL, "max_requests": 3}
        )

    data = result.structured_content
    assert data["execution"]["requests_used"] == 3
    assert data["execution"]["truncated"] is True
    assert data["records"] == []
    assert data["evaluated"] == 0
    reasons = [row["reason"] for row in data["unevaluated"]]
    assert reasons == ["budget", "budget"], (
        "one pipeline never had its stage history read, one never had its "
        "candidate's activity read - both unknown, neither a miss"
    )
    assert {row["pipeline_id"] for row in data["unevaluated"]} == {101, 102}
    assert data["execution"]["next_cursor"] is None, (
        "this sweep has no resumable position; `unevaluated` is what reports the gap"
    )
    assert data["execution"]["rate_limit"] == {"limit": None, "remaining": None}


async def test_max_rows_caps_the_records_but_never_the_reported_total():
    async with Client(build(account_handler)) as client:
        result = await client.call_tool(
            "find_followup_facts",
            {**BASE_CALL, "require_no_later_activity": False, "max_rows": 1},
        )

    data = result.structured_content
    assert data["count"] == 1
    assert data["total_matched"] == 2
    assert data["execution"]["truncated"] is True


async def test_candidates_can_seed_the_pool_instead_of_jobs():
    calls: list[str] = []

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/candidates/42/pipelines"):
            return httpx2.Response(200, json=collection([JOB_PIPELINES[0]], key="pipelines"))
        return account_handler(request)

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "find_followup_facts",
            {
                "candidate_ids": [42],
                "pipeline_status_ids": [INTERVIEW],
                "older_than_hours": 24,
                "as_of": AS_OF,
            },
        )

    assert "/v3/candidates/42/pipelines" in calls
    assert [row["pipeline_id"] for row in result.structured_content["records"]] == [101]


# --- the caller supplies the thresholds, always ------------------------------


async def test_there_is_no_default_number_of_hours():
    """Omitting the threshold is a validation error, not a call that silently
    applies somebody's 24. There is no number this adapter could pick that
    would not be one account's policy applied to every other."""
    async with Client(build(account_handler)) as client:
        with pytest.raises(Exception, match="older_than_hours"):
            await client.call_tool("find_followup_facts", {"job_ids": [7]})


async def test_a_call_with_no_pool_to_apply_the_threshold_to_is_refused():
    async with Client(build(account_handler)) as client:
        with pytest.raises(Exception, match="job_ids"):
            await client.call_tool("find_followup_facts", {"older_than_hours": 24})


# --- the typed contract (issue #17) ------------------------------------------


async def test_the_tool_declares_an_object_rooted_output_schema():
    """Without `output_schema=`, a tool returning ToolResult declares nothing.

    That is the issue #17 gap in its exact form: the call still works, the
    payload still arrives, and a caller reading the tool listing is told only
    that something comes back. Returning the model directly would declare a
    schema but also serialise the whole payload into display content, so the
    schema is passed explicitly and ToolResult is returned.
    """
    tool = await build(account_handler).get_tool("find_followup_facts")

    schema = tool.output_schema
    assert schema is not None, "find_followup_facts declares no output schema at all"
    assert schema.get("type") == "object", f"the schema root must be an object: {schema!r}"
    assert set(schema["properties"]) == EXPECTED_TOP_LEVEL, (
        "the declared top-level fields drifted from what callers depend on"
    )
    assert schema["properties"]["records"]["type"] == "array"

    execution = _resolve_ref(schema, schema["properties"]["execution"])
    assert execution.get("type") == "object"
    assert {"requests_used", "rate_limit", "truncated", "next_cursor", "errors"} <= set(
        execution["properties"]
    )

    row = _resolve_ref(schema, schema["properties"]["records"]["items"])
    assert {
        "candidate_id",
        "pipeline_id",
        "status_id",
        "status",
        "anchor_date",
        "anchor_date_field",
        "elapsed_hours",
        "last_activity_date",
    } <= set(row["properties"])

    criteria = _resolve_ref(schema, schema["properties"]["criteria"])
    assert {"older_than_hours", "comparison", "as_of", "later_comparison"} <= set(
        criteria["properties"]
    )


async def test_the_display_content_is_a_counts_line_not_a_second_copy():
    """The payload goes in structured content once. Repeating it in the display
    text doubles the tokens, and issue #21 keeps anything read off a person's
    record - names, notes, activity text - inside the structured result where a
    caller has to ask for it."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    assert len(result.content) == 1, "one summary line, not a block per record"
    text = result.content[0].text
    assert len(text.encode()) <= CONTENT_BYTE_CEILING, f"display content is not short: {text!r}"

    payload = json.dumps(result.structured_content)
    assert len(text.encode()) * 4 < len(payload.encode()), (
        "the display line is within a factor of four of the payload; it is a copy, "
        "not a summary"
    )

    for leaked in ("Dana", "Lee", "Priya", "Raman", "Artemis", "Interview"):
        assert leaked not in text, f"record data leaked into the display line: {leaked!r}"

    assert "matched 1" in text
    assert "requests" in text


async def test_no_candidate_name_or_note_reaches_the_payload_either():
    """Nothing here reads a candidate record or an activity note, and the row
    model has nowhere to put one. This is what keeps that true."""
    async with Client(build(account_handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    payload = json.dumps(result.structured_content)
    for leaked in ("Dana", "Priya", "Left a message", "Sent"):
        assert leaked not in payload, f"free text leaked into the payload: {leaked!r}"


# --- the boundary ------------------------------------------------------------


async def test_no_verdict_shaped_key_appears_anywhere_in_the_output():
    """Facts and arithmetic, at every nesting depth. A `priority` or a `score`
    here would be this tool quietly deciding what to do about a record after
    all - the exact temptation a follow-up list creates.

    Walks `structured_content` rather than `data`: `data` is a synthesised
    dataclass, and `_find_banned_keys` only descends dicts and lists, so
    pointing it at `data` would pass by walking nothing at all.
    """
    async with Client(build(account_handler)) as client:
        result = await client.call_tool("find_followup_facts", BASE_CALL)

    hits = _find_banned_keys(result.structured_content)
    assert not hits, f"find_followup_facts returned verdict-shaped keys: {hits}"
    assert BANNED_VERDICT_KEYS, "the banned-key list must not be empty"


async def test_no_verdict_shaped_key_is_declared_in_the_output_schema():
    """A field named `priority` on an empty result never appears in
    `structured_content`, so the payload check above would pass while the schema
    advertised the key to every caller. The declared contract is checked here."""
    tool = await build(account_handler).get_tool("find_followup_facts")

    properties = _schema_property_names(tool.output_schema)
    hits = sorted(name for name in properties if name.lower() in BANNED_VERDICT_KEYS)
    assert not hits, f"the output schema declares verdict-shaped fields: {hits}"


async def test_the_description_carries_no_recruiting_policy_vocabulary():
    """The same check tests/test_boundary.py applies to every registered tool,
    run here because this tool is not registered in create_server yet. A
    follow-up tool is where policy language wants to live most - including as a
    negation, which reads exactly the same to a substring match."""
    async with Client(build(account_handler)) as client:
        tools = {t.name: t for t in await client.list_tools()}

    text = (tools["find_followup_facts"].description or "").lower()
    hits = [word for word in TOOL_VOCAB if word in text]
    assert not hits, f"the description contains recruiting-policy vocabulary: {hits}"


async def test_the_tool_is_tagged_and_annotated_read_only():
    tool = await build(account_handler).get_tool("find_followup_facts")

    assert tool.tags == {"ats", "candidate", "pipeline", "activity", "read"}
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is True
