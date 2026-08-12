"""Prompts must teach the API, never the business.

Prompts are the primitive most likely to smuggle product logic into an adapter,
because a prompt *is* guidance for a model. The rule enforced here:

    A prompt may describe how to use the CATS API correctly.
    It may not describe what to do with the results.

The vocabulary check below is deliberately blunt. If a future prompt needs to
say "shortlist" or "prioritise", that is the signal it belongs in the
orchestrator, not that the test should be relaxed.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.server import create_server

#: Vocabulary of recruiting *judgement*. None of it belongs in this server.
POLICY_WORDS = (
    "shortlist",
    "prioriti",  # prioritise / prioritize / priority
    "best candidate",
    "top candidate",
    "rank",
    "score",
    "who to contact",
    "should contact",
    "reach out to",
    "recommend",
    "qualified for",
    "good fit",
    "screen out",
    "reject",
)

EXPECTED_PROMPTS = {
    "change_pipeline_stage_safely",
    "search_within_rate_budget",
    "find_the_right_custom_field",
    "record_an_external_interaction",
    "search_by_location",
}


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(
            api_key="test-key", base_url="https://api.catsone.com/v3", account_label="test"
        )

    def describe(self) -> str:
        return "stub"


def build():
    settings = Settings(api_key="test-key", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(
        settings,
        StubCredentials(),
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={})),
    )
    return create_server(settings, credential_provider=StubCredentials(), client=client)


async def prompt_text(client: Client, name: str, arguments: dict | None = None) -> str:
    result = await client.get_prompt(name, arguments or {})
    return " ".join(
        m.content.text if hasattr(m.content, "text") else str(m.content)
        for m in result.messages
    )


# --- registration ----------------------------------------------------------


async def test_expected_prompts_are_registered():
    async with Client(build()) as client:
        names = {p.name for p in await client.list_prompts()}
    assert EXPECTED_PROMPTS <= names


async def test_no_unexpected_prompts_were_added():
    """A new prompt should be a deliberate, reviewed decision."""
    async with Client(build()) as client:
        names = {p.name for p in await client.list_prompts()}
    assert names == EXPECTED_PROMPTS, f"unreviewed prompts: {sorted(names - EXPECTED_PROMPTS)}"


# --- the boundary ----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPTS))
async def test_prompt_contains_no_recruiting_judgement(name):
    async with Client(build()) as client:
        text = (await prompt_text(client, name)).lower()

    offending = [word for word in POLICY_WORDS if word in text]
    assert not offending, (
        f"prompt {name!r} contains recruiting-policy vocabulary {offending}. "
        f"Guidance about what to do with CATS data belongs in the orchestrator."
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPTS))
async def test_prompt_descriptions_are_also_clean(name):
    """The description is what a client shows a user, so it counts too."""
    async with Client(build()) as client:
        prompts = {p.name: p for p in await client.list_prompts()}

    description = (prompts[name].description or "").lower()
    offending = [word for word in POLICY_WORDS if word in description]
    assert not offending, f"prompt {name!r} description contains {offending}"


# --- content -------------------------------------------------------------


async def test_pipeline_prompt_warns_that_status_ids_are_account_specific():
    """The failure it prevents is silent: a valid-but-wrong id moves the record."""
    async with Client(build()) as client:
        text = await prompt_text(client, "change_pipeline_stage_safely")

    assert "cats://reference/workflows" in text
    assert "silently" in text.lower()


async def test_pipeline_prompt_explains_current_status_versus_history():
    async with Client(build()) as client:
        text = await prompt_text(client, "change_pipeline_stage_safely")

    assert "get_pipeline_statuses" in text
    assert "current" in text.lower()


async def test_rate_budget_prompt_points_at_the_batch_tools():
    async with Client(build()) as client:
        text = await prompt_text(client, "search_within_rate_budget")

    assert "500" in text
    for tool in ("get_candidate_summaries", "get_candidate_engagement", "get_changed_records"):
        assert tool in text


async def test_custom_field_prompt_explains_lookup_before_write():
    async with Client(build()) as client:
        text = await prompt_text(client, "find_the_right_custom_field")

    assert "cats://reference/custom-fields/candidates" in text
    assert "differ per account" in text


async def test_activity_prompt_states_that_this_server_does_not_send_anything():
    """The clearest boundary in the whole server: it records, it does not deliver."""
    async with Client(build()) as client:
        text = await prompt_text(client, "record_an_external_interaction")

    lowered = text.lower()
    assert "does not send" in lowered
    assert "no email" in lowered


async def test_prompts_accept_optional_context_without_changing_their_advice():
    async with Client(build()) as client:
        bare = await prompt_text(client, "change_pipeline_stage_safely")
        with_context = await prompt_text(
            client, "change_pipeline_stage_safely", {"candidate_id": "1367"}
        )

    assert "1367" in with_context
    # The guidance itself must be identical; context is appended, not substituted.
    assert bare.split("Context for this request")[0] in with_context
