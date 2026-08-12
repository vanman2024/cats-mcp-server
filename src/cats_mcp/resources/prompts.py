"""MCP prompts - how to operate this adapter correctly.

Prompts are the primitive most likely to smuggle product logic into an adapter,
because a prompt is literally guidance for a model. The rule applied here is
narrow and testable:

    A prompt in this server may describe how to use the CATS API correctly.
    It may not describe what to do with the results.

So "read the workflow statuses before changing a stage, because the ids are
account-specific" belongs here. "Decide which candidates to approach" does not,
and `tests/test_prompts.py` fails on the vocabulary of that second kind.

These exist because the CATS API has sharp edges that cost real money to learn:
a wrong status id silently moves a candidate to the wrong stage, and a naive
lookup loop burns an hourly request budget. A direct client - Claude, ChatGPT -
has no orchestrator to encode that for it.
"""

from __future__ import annotations

from typing import Any


def register(mcp: Any) -> int:
    """Register the API-usage prompts. Returns how many were added."""

    @mcp.prompt(
        name="change_pipeline_stage_safely",
        description=(
            "How to move a candidate's application to a different stage without "
            "using the wrong status id. Use before any pipeline status change."
        ),
        tags={"ats", "pipeline", "usage"},
    )
    def change_pipeline_stage_safely(job_id: str = "", candidate_id: str = "") -> str:
        target = ""
        if job_id or candidate_id:
            target = (
                f"\n\nContext for this request: "
                f"{'job_id=' + job_id if job_id else ''}"
                f"{' ' if job_id and candidate_id else ''}"
                f"{'candidate_id=' + candidate_id if candidate_id else ''}"
            )

        return (
            "Before changing any pipeline status in CATS, resolve the correct "
            "status id first.\n"
            "\n"
            "Status ids are specific to this CATS account and cannot be guessed "
            "or reused from another account. Passing a wrong-but-valid id does "
            "not error - it silently moves the record to a different stage.\n"
            "\n"
            "Steps:\n"
            "1. Read the resource cats://reference/workflows. It lists every "
            "workflow on this account and the id and title of each stage.\n"
            "2. Find the stage you intend by its title, and take its id.\n"
            "3. Call change_pipeline_status with that id.\n"
            "\n"
            "Two further points about pipeline state:\n"
            "- A pipeline's status field is its *current* stage only. To find "
            "out whether a record ever reached a stage, read its history with "
            "get_pipeline_statuses.\n"
            "- One candidate can hold several pipelines, one per job. Counting "
            "pipelines is not the same as counting people."
            f"{target}"
        )

    @mcp.prompt(
        name="search_within_rate_budget",
        description=(
            "How to search CATS and retrieve detail without exhausting the "
            "hourly request limit. Use before any bulk lookup."
        ),
        tags={"ats", "search", "usage"},
    )
    def search_within_rate_budget(what_you_are_looking_for: str = "") -> str:
        goal = (
            f"\n\nThe search in question: {what_you_are_looking_for}"
            if what_you_are_looking_for
            else ""
        )
        return (
            "The CATS API allows 500 requests per hour as standard - roughly "
            "eight per minute. Some accounts are raised; read the resource "
            "cats://account/rate-limit to see the real budget for this "
            "connection.\n"
            "\n"
            "How to stay inside it:\n"
            "1. Search once, not repeatedly. search_candidates matches free text "
            "across a profile; filter_candidates matches exact field values. "
            "Pick the one that fits rather than searching several times.\n"
            "2. Search results are compact by design and contain ids. That is "
            "the point - do not widen the search to get detail.\n"
            "3. For detail on many records, use the batch tools, not one call "
            "per record: get_candidate_summaries for profiles, "
            "get_candidate_engagement for contact history, "
            "get_job_candidate_pool for a job's pipeline.\n"
            "4. For detail on one record, use the specific tool: get_candidate, "
            "list_candidate_attachments, list_candidate_work_history.\n"
            "5. To find what has changed since last time, use get_changed_records "
            "rather than re-listing whole record sets.\n"
            "\n"
            "The batch tools report requests_used and the remaining budget, so "
            "check that before starting another batch."
            f"{goal}"
        )

    @mcp.prompt(
        name="find_the_right_custom_field",
        description=(
            "How to locate an account-specific custom field and its id before "
            "reading or writing its value."
        ),
        tags={"ats", "usage"},
    )
    def find_the_right_custom_field(field_name: str = "") -> str:
        looking_for = f"\n\nThe field being looked for: {field_name}" if field_name else ""
        return (
            "Custom fields hold the data this CATS account tracks beyond the "
            "standard fields. Their ids differ per account, so they must be "
            "looked up rather than assumed.\n"
            "\n"
            "Steps:\n"
            "1. Read cats://reference/custom-fields/candidates (or "
            "cats://reference/custom-fields/jobs). This lists every field with "
            "its id, name and type.\n"
            "2. Match the field by name and take its id.\n"
            "3. Read a value with get_candidate_custom_field, or write one with "
            "update_candidate_custom_field, passing that id.\n"
            "\n"
            "For dropdown and checkbox fields, read the field definition with "
            "get_candidate_custom_field_definition first to see which values are "
            "valid. Writing a value outside the configured options will not be "
            "accepted."
            f"{looking_for}"
        )

    @mcp.prompt(
        name="search_by_location",
        description=(
            "How to find people in a town or region without silently matching the "
            "wrong places. Use before any location-based candidate search."
        ),
        tags={"ats", "candidate", "search", "usage"},
    )
    def search_by_location(towns: str = "", within_km: str = "") -> str:
        target = ""
        if towns:
            target += f"\n\nTowns in scope: {towns}"
        if within_km:
            target += f"\nRadius: {within_km} km"

        return (
            "Two traps make location searches quietly return the wrong people.\n"
            "\n"
            "1. The 'contains' filter tokenizes the value and matches ANY token. "
            "Filtering city with contains='Logan Lake' also returns Williams Lake, "
            "Slave Lake and Deer Lake. contains='Cache Creek' returns every Creek. "
            "Nothing errors - the extra results simply look plausible.\n"
            "\n"
            "   Use filter='exactly' on the city field, one filter per "
            "municipality.\n"
            "\n"
            "2. Searching a single city excludes the surrounding communities people "
            "commute from. Establish the list of towns in scope first, or use the "
            "'geo_distance' filter to match a radius from a postal code - more "
            "accurate than any hand-written town list.\n"
            "\n"
            "The efficient shape, against a 500 requests/hour budget:\n"
            "\n"
            "1. One paginated filter_candidates call per town, city matched "
            "exactly.\n"
            "2. Keep results compact. Requesting every field returns enormous "
            "records for people you are about to discard.\n"
            "3. If certifications or other account-specific data decide the match, "
            "pass summary_level='standard' so custom fields arrive with the list, "
            "rather than fetching each candidate individually.\n"
            "4. Only load a full record, resume or activity history for someone who "
            "already matches.\n"
            "5. Deduplicate across towns by candidate id - one person can appear in "
            "more than one query."
            f"{target}"
        )

    @mcp.prompt(
        name="record_an_external_interaction",
        description=(
            "How to log that contact happened outside CATS - a call, email or "
            "message sent by another system - so the activity history stays "
            "accurate."
        ),
        tags={"ats", "activity", "usage"},
    )
    def record_an_external_interaction(candidate_id: str = "") -> str:
        who = f"\n\nCandidate: {candidate_id}" if candidate_id else ""
        return (
            "This server records activity in CATS. It does not send anything - "
            "no email, no SMS, no scheduling. Delivery is the calling system's "
            "responsibility; this only writes the record of it afterwards.\n"
            "\n"
            "To log an interaction:\n"
            "1. Call create_candidate_activity with the candidate id, the "
            "activity type, and notes describing what happened.\n"
            "2. Read it back with list_candidate_activities if you need to "
            "confirm the history.\n"
            "\n"
            "Keep the record faithful to what actually happened. An activity "
            "logged for an interaction that did not occur will later be read as "
            "genuine contact history by anyone - human or agent - looking at "
            "this candidate."
            f"{who}"
        )

    return 5
