"""Identity lookup: is this person already in CATS, and more than once?

Every other read primitive in this package answers "who is like this". This one
answers "who is this" - the question asked before a record is created, when two
records look like the same human being, or when an inbound application arrives
and nobody knows whether the account has already seen that person.

CATS makes that question expensive in three separate ways:

    * `POST /candidates/search` takes one field per request, so an identity made
      of five things is five requests, not one.
    * There is no normalized index behind any of them. A phone stored as
      "(250) 555 0111" does not match an exact filter for "250-555-0111", and an
      address stored as "Pat@Example.com" may or may not match "pat@example.com"
      depending on the field's collation.
    * Emails and phones are sub-collections. A search row does not carry them,
      so a row CATS hands back cannot be checked against what was asked for
      without a further read.

So the account gets swept, or the question gets answered by eyeballing a list.

`lookup_candidate` probes each identity value directly, then re-checks every row
it gets back by normalized equality before reporting it. That second step is the
one that matters: CATS decides what to surface, this decides what counts, and
the two are not the same. `contains` tokenises, `exactly` is only as exact as the
stored spelling, and neither knows that two phone numbers punctuated differently
are one number.

Normalization here is case, punctuation and whitespace and nothing else - an
email lowercased and trimmed, a phone reduced to its last ten digits, a name and
a URL with case and punctuation folded. There is no similarity metric and no
identity heuristic: two records either hold the same value or they do not.

What the caller gets back is a set of records and, for each, the fields that
matched and the values on both sides of the match. What that means - which
record is the one to keep, whether they are the same person at all, whether
anything should be done about it - is not decided here and is not knowable from
CATS alone.

Cost model:

    Phase A  one request per id given, plus one per identity probe. Bounded.
    Phase B  one record read per candidate any probe surfaced - only those.
    Phase C  a contact sub-collection read, only where the record did not carry
             the values a criterion needs to be checked against.

The ordering is the point: Phase C is the expensive one and it runs against the
few records that survived, on the one criterion that still needs an answer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from cats_mcp.composites.reads import (
    _dedupe,
    _embedded_rows,
    _gather_by_id,
    _has_next_page,
    _project,
)
from cats_mcp.http.correlation import get_logger, set_run_id
from cats_mcp.http.errors import CATSAPIError

logger = get_logger(__name__)

#: Rows per probe. An exact identity probe that overflows a page of 100 is
#: pathological - a last name shared by hundreds - and the response says so
#: rather than paging on, because paging an identity lookup spends a budget on
#: people the caller is not asking about.
PROBE_PAGE_SIZE = 100

#: Default and hard ceiling on requests for one call.
#:
#: Lower than the compound query's, because a lookup that needs dozens of
#: requests has stopped being a lookup. The CATS standard allowance is 500
#: requests per hour, and this question gets asked once per inbound record.
DEFAULT_MAX_REQUESTS = 20
MAX_REQUESTS_CEILING = 100

#: Trailing digits compared when two phone numbers are checked for equality.
#: Enough to make a country code and a trunk prefix irrelevant, short enough
#: that it is still the same subscriber number on both sides.
PHONE_SIGNIFICANT_DIGITS = 10

#: The CATS candidate field each identity kind is probed against.
#:
#: These are the documented spellings, but the account's own schema wins: emails
#: and phones are sub-resources, and whether the search endpoint accepts a flat
#: alias for one varies. A probe that CATS rejects is reported per probe in
#: `errors` and can be redirected with probe_field_overrides rather than
#: silently returning nothing.
PROBE_FIELDS: dict[str, str] = {
    "email": "email",
    "phone": "phone",
    "name": "last_name",
    "profile_url": "linkedin_url",
}

#: Candidate-record keys under which an email may be stored, inline or embedded.
#: All of them are read because reporting "no match" from having looked in one
#: place is a wrong answer on the exact field a duplicate check turns on.
EMAIL_KEYS = ("emails", "email", "email_address", "email1", "email2")
EMAIL_VALUE_KEYS = ("email", "address", "email_address", "value")

#: The same, for phones. `number` is the shape a live account returned; the
#: create endpoint's body calls it `phone`.
PHONE_KEYS = ("phones", "phone", "phone_cell", "phone_home", "phone_work", "mobile")
PHONE_VALUE_KEYS = ("phone", "number", "phone_number", "value")

#: And for profile URLs. CATS has no single canonical home for one: the create
#: endpoint takes `linkedin_url`, accounts also keep them on `website` or in a
#: custom field, which this does not read.
PROFILE_URL_KEYS = ("linkedin_url", "linkedin", "profile_url", "website", "url", "social")
URL_VALUE_KEYS = ("url", "link", "address", "value")

#: Record fields carried through to the result alongside the match evidence.
#: Enough to tell two records apart by eye; not a profile dump.
RECORD_FIELDS = ["title", "city", "state", "date_created", "date_modified"]

_PUNCTUATION = re.compile(r"[^\w\s]+")
_WHITESPACE = re.compile(r"\s+")
_NON_DIGITS = re.compile(r"\D+")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")


def _normalise_email(value: Any) -> str:
    """Lowercase and trim. Nothing else.

    Deliberately not the provider-specific folding that dot-strips or drops a
    '+tag': pat.lee@gmail.com and patlee@gmail.com are the same mailbox at one
    provider and two different people at another, so treating them as one would
    be a guess about identity rather than a normalization of spelling.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _digits(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _NON_DIGITS.sub("", value)


def _normalise_phone(value: Any) -> str:
    """Digits only, and the last ten of them.

    "250-555-0111", "(250) 555 0111" and "+1 250 555 0111" are one number
    written three ways, which is punctuation and a country code. A number
    carrying an extension is not covered - its digits run on past the subscriber
    number, so it normalises to something else and will not match.
    """
    digits = _digits(value)
    return digits[-PHONE_SIGNIFICANT_DIGITS:] if digits else ""


def _normalise_text(value: Any) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    Used for names. 'Pat O'Brien-Lee' and 'pat o brien lee' become one string,
    which is spelling. 'Lee, Pat' does not become 'Pat Lee' and a middle name
    does not disappear, because reordering or discarding a token is a claim
    about who someone is - pass the variants you mean, one per entry.
    """
    if not isinstance(value, str):
        return ""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", value.lower())).strip()


def _normalise_url(value: Any) -> str:
    """Lowercase, drop the scheme, a leading www. and a trailing slash.

    https://www.linkedin.com/in/pat-lee/ and linkedin.com/in/pat-lee are the
    same page addressed two ways. A query string is left alone: it is part of
    the address as stored, and deciding which parameters are decorative would
    be a guess.
    """
    if not isinstance(value, str):
        return ""
    text = _SCHEME.sub("", value.strip().lower())
    if text.startswith("www."):
        text = text[4:]
    return text.rstrip("/")


NORMALISERS: dict[str, Callable[[Any], str]] = {
    "email": _normalise_email,
    "phone": _normalise_phone,
    "name": _normalise_text,
    "profile_url": _normalise_url,
}


def _flatten(value: Any, value_keys: tuple[str, ...]) -> list[str]:
    """Every non-empty string in a value, whether it is one, a list, or rows."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten(item, value_keys))
        return out
    if isinstance(value, dict):
        return [
            value[key]
            for key in value_keys
            if isinstance(value.get(key), str) and value[key].strip()
        ]
    return []


def _collect(
    record: Any, container_keys: tuple[str, ...], value_keys: tuple[str, ...]
) -> list[str]:
    """Pull stored values out of a candidate record, inline or under _embedded.

    A full read returns emails and phones inline on some accounts and embedded
    on others, and a search row carries neither. Checking every shape costs
    nothing; checking one and reporting no match would be a silent wrong answer.
    """
    if not isinstance(record, dict):
        return []
    embedded = record.get("_embedded") if isinstance(record.get("_embedded"), dict) else {}
    out: list[str] = []
    for key in container_keys:
        value = record.get(key)
        if value is None and isinstance(embedded, dict):
            value = embedded.get(key)
        out.extend(_flatten(value, value_keys))
    return out


def _record_names(record: Any) -> list[str]:
    """The name spellings a record holds, as whole strings."""
    if not isinstance(record, dict):
        return []
    names: list[str] = []
    joined = " ".join(
        str(record.get(part) or "").strip() for part in ("first_name", "last_name")
    ).strip()
    if joined:
        names.append(joined)
    for key in ("full_name", "name"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            names.append(value)
    return names


def _probe_forms(kind: str, value: str) -> list[str]:
    """The spellings to ask CATS for, given one identity value.

    An exact filter matches the stored spelling, not the meaning, and CATS has
    no normalized index behind any of these fields. So each value is asked for
    as the caller wrote it and, where normalization produces a different string,
    as that too. Both are re-checked locally afterwards, which is what makes
    asking twice safe rather than a way of loosening the match.

    A name is the exception: the search endpoint takes one field per request and
    cannot AND a first name to a last one, so the probe is the last token and
    the whole name is confirmed against the record.
    """
    if kind == "name":
        tokens = _normalise_text(value).split()
        return [tokens[-1]] if tokens else []

    forms = [value]
    normalised = NORMALISERS[kind](value)
    if normalised and normalised not in forms:
        forms.append(normalised)
    return forms


def register(mcp: Any, client_getter: Callable[[], Any], *, enforce_auth: bool) -> int:
    """Register the identity lookup primitive. Returns how many were added."""
    tool_kwargs: dict[str, Any] = {}
    if enforce_auth:
        from fastmcp.server.auth import require_scopes

        tool_kwargs["auth"] = require_scopes("cats:read")

    @mcp.tool(
        name="lookup_candidate",
        description=(
            "Find every candidate record carrying a given identity - an id, an email "
            "address, a phone number, a name, or a profile URL - and report which of "
            "those fields matched on each one. Use it before creating a candidate, or "
            "when two records look like the same person, to see what the account "
            "already holds.\n\n"
            "CATS answers one field per search request and keeps no normalized index "
            "behind any of them, so a phone stored as '(250) 555 0111' does not answer "
            "an exact filter for '250-555-0111'. This asks for each value directly, "
            "then re-checks every row it gets back by normalized equality: an email "
            "lowercased and trimmed, a phone reduced to its last ten digits, a name and "
            "a URL with case and punctuation folded. A row that does not survive that "
            "check is reported as unconfirmed, not as a match.\n\n"
            "Normalization is spelling only. There is no similarity metric here - two "
            "records either hold the same value or they do not - so pass every variant "
            "you mean rather than expecting one to stand in for another.\n\n"
            "Each result carries matched_fields and the evidence behind it: the value "
            "asked for, the value stored on the record, and where it was read from. "
            "Interpreting them is the caller's job. This reports that two records share "
            "an email address; it does not say which of them is the one to keep, and it "
            "changes nothing.\n\n"
            "Budgeted: max_requests caps what one call may spend against the hourly "
            "allowance, and requests_used reports what it actually cost."
        ),
        tags={"ats", "candidate", "read", "search"},
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        **tool_kwargs,
    )
    async def lookup_candidate(
        candidate_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    "Candidate ids to read directly. One request each. An id that no "
                    "longer resolves is reported in errors rather than failing the call."
                )
            ),
        ] = None,
        emails: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Email addresses to look for. Compared lowercased and trimmed, so "
                    "'Pat@Example.com' finds a record storing 'pat@example.com'."
                )
            ),
        ] = None,
        phones: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Phone numbers to look for. Compared on digits alone, last ten, so "
                    "'250-555-0111' finds a record storing '(250) 555 0111'."
                )
            ),
        ] = None,
        names: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Whole names to look for, e.g. 'Pat Lee'. CATS cannot AND a first "
                    "name to a last one in a single search, so the last token is what is "
                    "asked for and the whole name is confirmed against the record. "
                    "Matching is exact after case and punctuation are folded: a middle "
                    "name or a reversed spelling is a different string, so pass each "
                    "variant you mean."
                )
            ),
        ] = None,
        profile_urls: Annotated[
            list[str] | None,
            Field(
                description=(
                    "LinkedIn or other profile URLs to look for. The scheme, a leading "
                    "'www.' and a trailing slash are ignored; a query string is not."
                )
            ),
        ] = None,
        probe_field_overrides: Annotated[
            dict[str, str] | None,
            Field(
                description=(
                    "Redirect a probe to a different CATS field, keyed by "
                    "'email', 'phone', 'name' or 'profile_url'. Defaults are "
                    + ", ".join(f"{k}={v!r}" for k, v in PROBE_FIELDS.items())
                    + ". Set one when this account stores an identity elsewhere and its "
                    "probe comes back as an error - matching is unaffected, only where "
                    "the candidates are asked for."
                )
            ),
        ] = None,
        max_requests: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_REQUESTS_CEILING,
                description=(
                    f"CATS requests this call may spend. Ceiling {MAX_REQUESTS_CEILING}. "
                    "Work stops here and the response says what was left undone."
                ),
            ),
        ] = DEFAULT_MAX_REQUESTS,
    ) -> dict[str, Any]:
        set_run_id()
        client = client_getter()

        criteria: dict[str, list[str]] = {
            "email": [str(v).strip() for v in (emails or []) if str(v).strip()],
            "phone": [str(v).strip() for v in (phones or []) if str(v).strip()],
            "name": [str(v).strip() for v in (names or []) if str(v).strip()],
            "profile_url": [str(v).strip() for v in (profile_urls or []) if str(v).strip()],
        }
        wanted_ids = _dedupe(candidate_ids or [])

        if not wanted_ids and not any(criteria.values()):
            raise ValueError(
                "An identity is required: pass candidate_ids, emails, phones, names or "
                "profile_urls. Without one this would sweep every candidate in the "
                "account, which the request budget exists to prevent."
            )

        fields = dict(PROBE_FIELDS)
        for kind, field in (probe_field_overrides or {}).items():
            key = str(kind).strip().lower()
            if key not in fields:
                raise ValueError(
                    f"Unknown probe_field_overrides key {kind!r}. Valid keys: "
                    f"{sorted(PROBE_FIELDS)}."
                )
            if str(field).strip():
                fields[key] = str(field).strip()

        # normalized value -> the caller's spelling of it, so evidence can quote
        # what was actually asked for rather than the folded form.
        targets: dict[str, dict[str, str]] = {
            kind: {NORMALISERS[kind](v): v for v in values if NORMALISERS[kind](v)}
            for kind, values in criteria.items()
        }

        errors: dict[str, str] = {}
        requests_used = 0
        truncated = False
        found: dict[str, dict[str, Any]] = {}

        def note_found(candidate_id: Any, kind: str, value: str, row: Any = None) -> None:
            entry = found.setdefault(
                str(candidate_id), {"found_by": [], "record": None, "row": None}
            )
            if row is not None and entry["row"] is None:
                entry["row"] = row
            if {"field": kind, "value": value} not in entry["found_by"]:
                entry["found_by"].append({"field": kind, "value": value})

        async def fetch_record(cid: int | str) -> Any:
            return await client.request("GET", f"/candidates/{cid}")

        # --- Phase A1: ids the caller already has ---------------------------
        if wanted_ids:
            affordable = wanted_ids[: max(0, max_requests - requests_used)]
            if len(affordable) < len(wanted_ids):
                truncated = True
                errors["candidate_ids"] = (
                    f"budget covered {len(affordable)} of {len(wanted_ids)} ids; "
                    f"raise max_requests to read the rest"
                )
            records, id_errors = await _gather_by_id(affordable, fetch_record)
            requests_used += len(affordable)
            for key, message in id_errors.items():
                errors[f"candidate:{key}"] = message
            for key, record in records.items():
                note_found(key, "candidate_id", key)
                found[key]["record"] = record

        # --- Phase A2: one probe per identity spelling -----------------------
        plan: list[tuple[str, str, str, str]] = []
        asked: set[tuple[str, str]] = set()
        for kind, values in criteria.items():
            for value in _dedupe(values):
                for form in _probe_forms(kind, str(value)):
                    key = (fields[kind], form)
                    if key in asked:
                        continue
                    asked.add(key)
                    plan.append((kind, str(value), fields[kind], form))

        probes: list[dict[str, Any]] = []
        for index, (kind, value, field, form) in enumerate(plan):
            if requests_used >= max_requests:
                truncated = True
                errors["probes"] = (
                    f"budget stopped after {index} of {len(plan)} probes; the identities "
                    f"they cover were not looked for at all"
                )
                break

            try:
                payload = await client.request(
                    "POST",
                    "/candidates/search",
                    json={"field": field, "filter": "exactly", "value": form},
                    params={"per_page": PROBE_PAGE_SIZE, "page": 1},
                )
                requests_used += 1
            except CATSAPIError as exc:
                errors[f"probe:{field}={form}"] = str(exc)
                probes.append({"field": field, "value": form, "for": kind, "rows": None})
                continue

            rows = _embedded_rows(payload)
            probes.append({"field": field, "value": form, "for": kind, "rows": len(rows)})
            if _has_next_page(payload):
                truncated = True
                errors[f"probe:{field}={form}"] = (
                    f"more than {PROBE_PAGE_SIZE} records answered this probe; only the "
                    f"first page was examined, so this identity may appear on records "
                    f"not listed here"
                )
            for row in rows:
                identifier = row.get("id")
                if identifier is not None:
                    note_found(identifier, kind, value, row=row)

        # --- Phase B: read the records the probes surfaced -------------------
        need_record = [cid for cid, entry in found.items() if entry["record"] is None]
        if need_record:
            affordable = need_record[: max(0, max_requests - requests_used)]
            if len(affordable) < len(need_record):
                truncated = True
                errors["records"] = (
                    f"budget covered {len(affordable)} of {len(need_record)} records "
                    f"found; the rest were checked against their search row only"
                )
            records, record_errors = await _gather_by_id(affordable, fetch_record)
            requests_used += len(affordable)
            for key, message in record_errors.items():
                errors[f"record:{key}"] = message
            for key, record in records.items():
                found[key]["record"] = record

        # --- Phase C: contact sub-collections, only where still unanswered ---
        # A search row carries neither emails nor phones, and a full read carries
        # them only on some accounts. This spends a request exactly where a
        # criterion would otherwise go unanswered, which is the difference
        # between "no match" and "not checked".
        extra: dict[str, dict[str, list[str]]] = {cid: {} for cid in found}
        for kind, endpoint, container, value_keys in (
            ("email", "emails", EMAIL_KEYS, EMAIL_VALUE_KEYS),
            ("phone", "phones", PHONE_KEYS, PHONE_VALUE_KEYS),
        ):
            if not targets[kind]:
                continue
            pending = [
                cid
                for cid, entry in found.items()
                if not _collect(entry["record"] or entry["row"], container, value_keys)
            ]
            if not pending:
                continue

            affordable = pending[: max(0, max_requests - requests_used)]
            if len(affordable) < len(pending):
                truncated = True
                errors[endpoint] = (
                    f"budget covered {len(affordable)} of {len(pending)} {endpoint} "
                    f"sub-collections; the rest could not be checked for a {kind} match"
                )

            async def fetch_sub(cid: int | str, path: str = endpoint) -> Any:
                return await client.request(
                    "GET", f"/candidates/{cid}/{path}", params={"per_page": PROBE_PAGE_SIZE}
                )

            payloads, sub_errors = await _gather_by_id(affordable, fetch_sub)
            requests_used += len(affordable)
            for key, message in sub_errors.items():
                errors[f"{endpoint}:{key}"] = message
            for key, payload in payloads.items():
                extra[key][kind] = _flatten(_embedded_rows(payload), value_keys)

        # --- confirm, and say what the confirmation rested on ----------------
        rows_out: list[dict[str, Any]] = []
        unconfirmed: list[dict[str, Any]] = []

        for cid, entry in sorted(found.items(), key=lambda item: str(item[0])):
            record = entry["record"]
            source = "candidate record" if record is not None else "search row"
            record = record if record is not None else (entry["row"] or {})

            stored: dict[str, list[tuple[str, str]]] = {
                "email": [
                    (v, source) for v in _collect(record, EMAIL_KEYS, EMAIL_VALUE_KEYS)
                ]
                + [(v, "emails sub-collection") for v in extra.get(cid, {}).get("email", [])],
                "phone": [
                    (v, source) for v in _collect(record, PHONE_KEYS, PHONE_VALUE_KEYS)
                ]
                + [(v, "phones sub-collection") for v in extra.get(cid, {}).get("phone", [])],
                "name": [(v, source) for v in _record_names(record)],
                "profile_url": [
                    (v, source) for v in _collect(record, PROFILE_URL_KEYS, URL_VALUE_KEYS)
                ],
            }

            matched_fields: list[str] = []
            evidence: list[dict[str, Any]] = []

            if any(f["field"] == "candidate_id" for f in entry["found_by"]):
                matched_fields.append("candidate_id")
                evidence.append(
                    {
                        "field": "candidate_id",
                        "asked_for": cid,
                        "stored": record.get("id", cid),
                        "normalized": str(cid),
                        "read_from": "candidate record",
                    }
                )

            for kind, wanted in targets.items():
                if not wanted:
                    continue
                for value, read_from in stored[kind]:
                    normalized = NORMALISERS[kind](value)
                    if not normalized or normalized not in wanted:
                        continue
                    if kind not in matched_fields:
                        matched_fields.append(kind)
                    evidence.append(
                        {
                            "field": kind,
                            "asked_for": wanted[normalized],
                            "stored": value,
                            "normalized": normalized,
                            "read_from": read_from,
                        }
                    )

            if not matched_fields:
                unconfirmed.append(
                    {
                        "candidate_id": record.get("id", cid),
                        "found_by": entry["found_by"],
                        "reason": (
                            "the record holds no value equal to any identity asked for, "
                            "once normalized"
                            if entry["record"] is not None
                            else "the full record could not be read within the request "
                            "budget, so only its search row was checked"
                        ),
                    }
                )
                continue

            out: dict[str, Any] = {
                "candidate_id": record.get("id", cid),
                "name": (_record_names(record) or [None])[0],
            }
            out.update(_project(record, RECORD_FIELDS))
            out["emails"] = _dedupe([v for v, _ in stored["email"]])
            out["phones"] = _dedupe([v for v, _ in stored["phone"]])
            out["profile_urls"] = _dedupe([v for v, _ in stored["profile_url"]])
            out["matched_fields"] = matched_fields
            out["evidence"] = evidence
            out["found_by"] = entry["found_by"]
            out["read_from"] = source
            rows_out.append(out)

        return {
            "candidates": rows_out,
            "count": len(rows_out),
            "unconfirmed": unconfirmed,
            "asked_for": {
                **({"candidate_ids": wanted_ids} if wanted_ids else {}),
                **{kind: values for kind, values in criteria.items() if values},
            },
            "probes": probes,
            "truncated": truncated,
            "errors": errors,
            "requests_used": requests_used,
            "rate_limit": client.rate_limit.snapshot(),
            "note": (
                "Ordered by candidate id, which is record order and nothing more. "
                "`matched_fields` names the fields that matched and `evidence` quotes "
                "both sides of each one; what that means about these records is the "
                "caller's to decide. More than one row here means the account holds "
                "more than one record carrying an identity you asked about - it does "
                "not mean anything has been changed, and nothing has. `unconfirmed` "
                "lists records CATS surfaced whose stored values did not survive the "
                "normalized check, and a non-empty `errors` means part of the question "
                "went unanswered rather than answered in the negative."
            ),
        }

    return 1
