"""The fact-versus-judgment boundary, enforced across the whole tool surface.

`tests/test_prompts.py` proved the pattern for prompt text: a blunt vocabulary
of recruiting judgement, checked against everything a model or a human reads.
This file generalizes the same idea to the rest of what the server exposes -
tool names, tool descriptions, composite outputs, and the specs that generate
them - because a prompt is not the only place product logic can be smuggled
in. A tool named `find_best_candidates` or a response key called `excluded`
would be exactly as much of a boundary violation as a prompt that says
"reach out to".

See docs/ARCHITECTURE.md, "Facts versus judgment: a test for new work", for
the full argument this file enforces.
"""

from __future__ import annotations

import ast
import pathlib

import httpx2
import pytest
from fastmcp import Client
from test_prompts import POLICY_WORDS

from cats_mcp.composites.reads import MAX_BATCH
from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.server import create_server

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "cats_mcp"

#: The prompts vocabulary, extended with "exclud" (excluded/exclusion/excludes).
#: Extended rather than duplicated so the two files cannot drift: a word added
#: to test_prompts.POLICY_WORDS is automatically checked here too.
#:
#: "exclud" is not in the prompts list because no prompt has ever needed the
#: word; the architecture doc's own contrasting pair ("placed people are
#: excluded" is a customer's rule) makes it exactly the kind of vocabulary this
#: file exists to catch on the tool surface.
TOOL_VOCAB: tuple[str, ...] = POLICY_WORDS + ("exclud",)

#: Vocabulary hits read in context and confirmed NOT to violate the boundary,
#: because the tool names a real CATS domain concept (an account's own
#: pipeline stage is literally titled "Rejected"; a saved list is colloquially
#: a "shortlist") or explicitly attributes the decision to the caller rather
#: than rendering one itself ("whoever your rules exclude"). Each entry below
#: was inspected before being added; a hit this dict does not name still fails
#: the test, including a *new* occurrence of an already-listed word on a
#: different tool.
VERIFIED_SAFE_USAGE: dict[str, frozenset[str]] = {
    # "Lists are static groupings of candidates, useful as shortlists or
    # campaign audiences." - describes what a saved list can be used for, not
    # who is on one.
    "list_candidate_lists": frozenset({"shortlist"}),
    # "Use this to review the shortlist for a role or see who is under
    # consideration." - reports who is in a pipeline; renders no verdict.
    "list_job_pipelines": frozenset({"shortlist"}),
    # "Add a candidate to a job's pipeline - submit or shortlist them for that
    # role." - the caller performs the action; the tool does not choose whom.
    "create_pipeline": frozenset({"shortlist"}),
    # "Move a candidate's application to a different stage ... for example to
    # interviewing, submitted to hiring manager, offered, placed or rejected."
    # "Rejected" is a real CATS pipeline stage name, offered only as an example
    # of a valid status; the caller supplies the target status id either way.
    "change_pipeline_status": frozenset({"reject"}),
    # "discard whoever your rules exclude" - explicitly the caller's rules,
    # not the adapter's. "Recommended order: screen the whole set with
    # include=['lists'] ..." recommends a *call sequence* (sequencing, which
    # is this server's business per docs/ARCHITECTURE.md), not a candidate.
    # This is the boundary stated correctly, not crossed.
    "get_candidate_context": frozenset({"exclud", "recommend"}),
    # "Returns facts only; deciding who to contact is the caller's decision."
    # States the boundary explicitly rather than crossing it.
    "get_candidate_engagement": frozenset({"who to contact"}),
}


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(
            api_key="test-key", base_url="https://api.catsone.com/v3", account_label="test"
        )

    def describe(self) -> str:
        return "stub"


def build(handler=None):
    settings = Settings(api_key="test-key", discovery_mode=DiscoveryMode.RAW)
    transport = httpx2.MockTransport(handler or (lambda request: httpx2.Response(200, json={})))
    client = CATSClient(settings, StubCredentials(), transport=transport)
    return create_server(settings, credential_provider=StubCredentials(), client=client)


# --- tool descriptions ------------------------------------------------------


async def test_no_tool_description_contains_recruiting_judgement():
    """A tool description is guidance a model reads, exactly like a prompt.

    Breaking this means a tool's own description started telling the caller
    who to contact, who is excluded, or who is a good fit - the decision this
    server exists to leave to the orchestrator. The vocabulary is the same one
    tests/test_prompts.py enforces for prompts, because there is nothing
    special about a tool description that makes smuggled policy language less
    dangerous there than in a prompt.
    """
    async with Client(build()) as client:
        tools = await client.list_tools()

    violations: dict[str, list[str]] = {}
    for tool in tools:
        text = (tool.description or "").lower()
        allowed = VERIFIED_SAFE_USAGE.get(tool.name, frozenset())
        hits = [word for word in TOOL_VOCAB if word in text and word not in allowed]
        if hits:
            violations[tool.name] = hits

    assert not violations, (
        f"tool descriptions contain recruiting-policy vocabulary: {violations}. "
        f"Guidance about what CATS data means belongs in the orchestrator, not "
        f"in a tool description a model reads to decide what to do."
    )


async def test_verified_safe_usage_still_matches_real_tools():
    """Guards the allowlist above against going stale.

    If a description changes and no longer contains the word it was
    allowlisted for, the entry is dead weight that would silently hide a real
    future violation of the same word on the same tool. Catching that here
    means the allowlist stays an accurate record of what was actually checked.
    """
    async with Client(build()) as client:
        by_name = {t.name: t for t in await client.list_tools()}

    stale = []
    for name, words in VERIFIED_SAFE_USAGE.items():
        assert name in by_name, f"allowlisted tool {name!r} no longer exists"
        text = (by_name[name].description or "").lower()
        for word in words:
            if word not in text:
                stale.append((name, word))

    assert not stale, f"allowlist entries no longer match tool text: {stale}"


# --- tool names --------------------------------------------------------------

#: Substrings and prefixes that encode a policy verb in a tool name itself.
#: The neutral equivalent already exists for the one concrete case this
#: project has needed - create_candidate_list_items adds to a list whose id
#: the caller chose, rather than a tool deciding who belongs on it.
POLICY_NAME_PATTERNS: tuple[str, ...] = (
    "rank_",
    "_rank",
    "should_",
    "qualify_",
    "qualifies_",
    "find_best",
    "best_candidate",
    "top_candidate",
    "recommend_",
    "score_",
    "_score",
    "screen_out",
    "exclude_",
    "do_not_contact",
    "good_fit",
    "best_fit",
)


async def test_no_tool_name_encodes_a_policy_verb():
    """A tool NAME is the one piece of an MCP surface a caller acts on without
    reading anything else first - a name like `set_do_not_contact` or
    `find_best_candidates` invites exactly the misuse this server refuses to
    support: a customer's rule baked into the verb, applied to every other
    customer on the same adapter. `create_candidate_list_items` is the pattern
    to follow instead - it adds to a list whose id the caller chose, and says
    nothing about what that list means.
    """
    async with Client(build()) as client:
        names = {t.name for t in await client.list_tools()}

    offenders = {
        name: pattern
        for name in names
        for pattern in POLICY_NAME_PATTERNS
        if pattern in name.lower()
    }
    assert not offenders, f"tool names encode a policy verb: {offenders}"


# --- composite outputs carry facts, not verdicts -----------------------------

#: tests/test_candidate_context.py asserts this for get_candidate_context
#: alone; this is that same list, generalized across every composite tool.
BANNED_VERDICT_KEYS: frozenset[str] = frozenset(
    {
        "eligibility",
        "excluded",
        "exclusion_reasons",
        "fit",
        "good_fit",
        "recommendation",
        "recommended",
        "score",
        "rank",
        "ranking",
        "priority",
        "qualified",
        "verdict",
        "should_contact",
    }
)


def _find_banned_keys(value, path="") -> list[str]:
    """Walk a tool result recursively, collecting any banned key with its path."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.lower() in BANNED_VERDICT_KEYS:
                found.append(key_path)
            found.extend(_find_banned_keys(sub, key_path))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found.extend(_find_banned_keys(item, f"{path}[{i}]"))
    return found


#: A minimal but structurally real PDF, for find_candidate_resume's download leg.
RESUME_PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
)


def _stub_handler(request):
    """One handler covering every endpoint the composite tools call.

    Enough shape to exercise real code paths - list projection, activity
    aggregation, status-title resolution - without a live CATS account.
    """
    path = request.url.path
    if path.endswith("/pipelines/workflows"):
        return httpx2.Response(
            200,
            json={
                "_embedded": {
                    "workflows": [{"id": 1, "statuses": [{"id": 3, "title": "Screening"}]}]
                }
            },
        )
    if path.endswith("/activities"):
        return httpx2.Response(
            200,
            json={
                "total": 1,
                "_embedded": {
                    "activities": [{"date_created": "2024-01-01", "type": "call"}]
                },
            },
        )
    if path.endswith("/pipelines") and "jobs" in path:
        return httpx2.Response(
            200,
            json={
                "total": 1,
                "_embedded": {
                    "pipelines": [
                        {"id": 1, "candidate_id": 42, "job_id": 7, "status_id": 3, "rating": 5}
                    ]
                },
            },
        )
    if path.endswith("/pipelines") and "candidates" in path:
        return httpx2.Response(
            200,
            json={"_embedded": {"pipelines": [{"id": 1, "job_id": 7, "status_id": 3}]}},
        )
    if path.endswith("/events"):
        return httpx2.Response(
            200,
            json={
                "_embedded": {
                    "events": [
                        {"id": 9, "event": "candidate.created", "regarding_id": 42,
                         "date_created": "2024-01-01"}
                    ]
                }
            },
        )
    if "/pipelines/" in path:
        return httpx2.Response(
            200,
            json={"id": 1, "candidate_id": 42, "job_id": 7, "status_id": 3, "rating": 5},
        )
    if path.endswith("/attachments"):
        return httpx2.Response(
            200,
            json={
                "_embedded": {
                    "attachments": [
                        {
                            "id": 99,
                            "filename": "dana-lee-resume.pdf",
                            "is_resume": True,
                            "date_created": "2024-06-01",
                        }
                    ]
                }
            },
        )
    if path.endswith("/attachments/99/download"):
        return httpx2.Response(
            200,
            content=RESUME_PDF_BYTES,
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": 'attachment; filename="dana-lee-resume.pdf"',
            },
        )
    if "/candidates/" in path:
        return httpx2.Response(
            200,
            json={
                "id": 42,
                "first_name": "Dana",
                "last_name": "Lee",
                "title": "Welder",
                "city": "Kamloops",
                "state": "BC",
                "is_hot": False,
                "date_modified": "2024-01-01",
            },
        )
    return httpx2.Response(200, json={})


#: (tool name, arguments) for every composite read primitive. Kept in one
#: place so a new composite is an obvious addition to this list, not a gap.
COMPOSITE_CALLS: tuple[tuple[str, dict], ...] = (
    ("get_candidate_summaries", {"candidate_ids": [42]}),
    ("get_candidate_engagement", {"candidate_ids": [42]}),
    ("get_job_candidate_pool", {"job_id": 7}),
    ("get_changed_records", {"since_event_id": 0}),
    ("get_pipeline_summaries", {"pipeline_ids": [1]}),
    ("get_candidate_context", {"candidate_ids": [42], "include": ["identity", "pipelines"]}),
    ("get_candidate_activity", {"candidate_ids": [42]}),
    ("find_candidate_resume", {"candidate_id": 42}),
    ("query_candidate_facts", {"states": ["BC"]}),
    ("lookup_candidate", {"emails": ["pat@example.com"]}),
    ("resolve_job", {"job_id": 7}),
    ("get_candidate_timeline", {"candidate_ids": [42]}),
)


@pytest.mark.parametrize(
    "tool_name,arguments", COMPOSITE_CALLS, ids=[c[0] for c in COMPOSITE_CALLS]
)
async def test_composite_outputs_carry_no_verdict_shaped_keys(tool_name, arguments):
    """Deciding what candidate data means belongs to the caller, not here.

    tests/test_candidate_context.py already proves this for
    get_candidate_context on its own; breaking this test on a *different*
    composite means a new tool started returning a key like `excluded` or
    `score` - rendering a judgement instead of reporting the facts (list
    membership, pipeline stage, activity dates) the caller needs to make one.
    """
    async with Client(build(_stub_handler)) as client:
        result = await client.call_tool(tool_name, arguments)

    hits = _find_banned_keys(result.data)
    assert not hits, f"{tool_name} returned verdict-shaped keys: {hits}"


async def test_every_composite_tool_is_covered_above():
    """Guards COMPOSITE_CALLS itself: a new composite with no entry here would
    silently skip the verdict-key check above instead of failing it.

    Composite tool names are derived, not hardcoded: everything registered in
    `raw` discovery mode that is neither a declarative REGISTRY tool nor the
    connection-status tool is a composite, by construction of `create_server`.
    That derivation is what makes this guard actually guard something - two
    independently hardcoded lists would agree by construction and never catch
    a real gap, which a first draft of this test did.
    """
    async with Client(build()) as client:
        all_names = {t.name for t in await client.list_tools()}

    registry_names = {spec.name for spec in REGISTRY}
    composite_names = all_names - registry_names - {"get_connection_status"}

    covered = {name for name, _ in COMPOSITE_CALLS}
    assert covered == composite_names, (
        f"COMPOSITE_CALLS is out of sync with the registered composite tools: "
        f"missing={composite_names - covered}, extra={covered - composite_names}"
    )


# --- no account-specific identifier is baked into a spec or composite -------


def _int_literals(path: pathlib.Path) -> list[tuple[int, int]]:
    """(line, value) for every integer literal in a source file's AST.

    AST rather than text search: a docstring or comment mentioning an example
    id, like "6377104" in composites/reads.py, is prose to a reader and must
    not trip this test. A real hardcoded default or constant is a literal
    Python value, which only the AST sees.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            if isinstance(node.value, bool):
                continue
            out.append((node.lineno, node.value))
    return out


#: (relative path, line, value) verified to be a unit conversion or a
#: structural limit, not an account-specific record id. CATS ids in this
#: codebase's own test fixtures run 6-9 digits (1600001, 400000001,
#: 6377104); nothing legitimate here comes close.
KNOWN_LARGE_LITERALS: frozenset[tuple[str, int, int]] = frozenset(
    {
        # 1MB, used to size the inline-attachment limit and its error message.
        ("registry/build.py", 486, 1_048_576),
        ("registry/build.py", 487, 1_048_576),
    }
)

#: Below this, a literal is assumed to be a page size, a batch limit, or
#: similar structural constant (MAX_BATCH=50, LIST_PAGE_SIZE=100, and so on),
#: never a CATS record id.
ID_LIKE_THRESHOLD = 10_000


def test_no_hardcoded_account_specific_id_in_src():
    """A list id, status id, or similar is specific to one CATS account.

    Baking one into a spec or composite - as a default, a constant, or a
    silent fallback - means every other account using this same adapter would
    silently have that customer's list or status applied to them too. Scoped
    to src/ only: tests and docstrings legitimately cite real-looking ids as
    worked examples (see composites/reads.py's own docstrings), and flagging
    those would make this test noise instead of signal.
    """
    assert MAX_BATCH < ID_LIKE_THRESHOLD, "recalibrate ID_LIKE_THRESHOLD: MAX_BATCH crossed it"

    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for line, value in _int_literals(path):
            if value < ID_LIKE_THRESHOLD:
                continue
            if (rel, line, value) in KNOWN_LARGE_LITERALS:
                continue
            offenders.append(f"{rel}:{line} = {value}")

    assert not offenders, (
        f"possible hardcoded account-specific id in src/: {offenders}. If this "
        f"is a genuine structural constant (a byte size, a rate-limit ceiling), "
        f"add it to KNOWN_LARGE_LITERALS with a comment explaining what it is."
    )


def test_no_spec_default_supplies_an_id_the_caller_did_not_choose():
    """An id-shaped parameter (list_id, status_id, tag_id, ...) must always
    come from the caller. A hardcoded default would mean the tool silently
    acts against one specific account's list or status whenever the caller
    omits the argument - the exact failure mode of one customer's data
    quietly becoming every customer's default.
    """
    offenders = []
    for spec in REGISTRY:
        for param in spec.params:
            name = param.name.lower()
            is_id_param = name.endswith("_id") or name.endswith("_ids") or name == "id"
            if is_id_param and param.default is not None and param.required is False:
                offenders.append(f"{spec.name}.{param.name} defaults to {param.default!r}")

    assert not offenders, f"id parameters with a hardcoded default: {offenders}"


def test_no_constant_body_carries_an_account_specific_value():
    """`constant_body` fills in values CATS requires that the caller never
    supplies (see registry/models.py). It exists for fixed, account-agnostic
    requirements like `{"type": "job"}` on a list-creation call - not as a
    back door for baking one account's list id or status id into every call a
    tool makes.
    """
    offenders = []
    for spec in REGISTRY:
        for key, value in spec.constant_body:
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= ID_LIKE_THRESHOLD:
                offenders.append(f"{spec.name}.constant_body[{key!r}] = {value!r}")

    assert not offenders, f"constant_body carries an id-shaped value: {offenders}"
