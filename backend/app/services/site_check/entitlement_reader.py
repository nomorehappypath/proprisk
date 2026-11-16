"""entitlement_reader — read a city's own planning pages for live cases.

Most small Texas cities publish their development pipeline ONLY as human web
pages: a Planning & Development page, an annexation table, a P&Z agenda PDF.
There is no feed to query, so the deterministic entitlement adapter can offer
nothing but a link, and the card reads as "nothing found" when in fact nothing
was ever *asked*. This module asks — a web-search-capable OpenAI model reads
the city's published pages and returns the cases it can actually see.

The design problem with letting a model answer "what is being built near
here?" is that a plausible-sounding development case is exactly the kind of
thing a model will invent. So NOTHING the model says is trusted:

  1. every case must carry a `source_url`;
  2. this module RE-FETCHES that URL server-side (HTML or PDF) and confirms
     the case name actually appears in the retrieved text;
  3. a case that fails is DROPPED, logged, and counted in `dropped_unverified`,
     which the card states out loud.

A case with no verified source never reaches the payload. That guard is the
point of the module — the model is a search-and-read tool, and the fetch is
the fact-checker.

Everything else follows the house AI pattern (narratives.py §11.3,
historical_aerials.py §19): one metered call through the gateway
(reserve → call → settle), the FULL degradation ladder, and a run that never
fails — the deterministic county plat records always still render.
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any

import requests
from openai import OpenAI

from app.models import Tenant
from app.services import gateway
from app.services.site_check import source_cache
from app.services.site_check.adapters._base import http_session
from app.services.site_check.narratives import (
    MSG_BUDGET_CAP,
    MSG_INVALID_KEY,
    MSG_NO_KEY,
    MSG_UNAVAILABLE,
)

log = logging.getLogger("propdesk.site_check.entitlement_reader")

SOURCE_ID = "entitlement_city_cases"
PRODUCER_VERSION = 1
APP_SLUG = "site-check"

# The card slot these cases are merged into.
ENTITLEMENT_SOURCE_ID = "entitlement_pipeline"

MSG_NO_PORTAL = (
    "PropDesk has no planning page on file for this city, so there was "
    "nothing to read."
)
MSG_ALL_DROPPED = (
    "PropDesk could not confirm any of the cases it found against the city's "
    "own pages, so none are shown. The county records above are unaffected."
)
MSG_NONE_FOUND = (
    "PropDesk read this city's planning pages and found no development cases "
    "posted near this site."
)

_MAX_FIELD_CHARS = 400
_MAX_QUOTE_CHARS = 240
# Fields that render inside a table cell get a tighter budget than free text.
_MAX_SHORT_CHARS = 48
_MAX_CELL_CHARS = 90
_MAX_FETCH_BYTES = 8 * 1024 * 1024
_MIN_PARTIAL_NAME_CHARS = 8
_MIN_PARTIAL_NAME_TOKENS = 2

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_MARKUP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")

CASE_FIELDS = (
    "name",
    "case_type",
    "status",
    "date",
    "applicant_or_developer",
    "location_text",
    "source_url",
    "evidence_quote",
)

_SYSTEM_PROMPT = (
    "You are a real-estate diligence researcher finding DEVELOPMENT CASES "
    "near a specific property. Small Texas cities do not publish a queryable "
    "case feed — the cases live on city web pages: Planning & Development "
    "pages, current-case or project lists, annexation tables, Planning & "
    "Zoning and City Council agendas and minutes, and permit or "
    "capital-project pages. USE WEB SEARCH and actually open the city's own "
    "pages.\n\n"
    "REPORT: rezonings, site plans, subdivision or plat cases, PUDs, "
    "annexations, and construction permits for multifamily or residential "
    "subdivisions, that are ACTIVE or were decided recently.\n\n"
    "EVIDENCE RULE — the single most important rule: report a case ONLY if "
    "you opened a page that names it. For each case, `source_url` must be the "
    "exact page you read it on, and `evidence_quote` must be a SHORT snippet "
    "copied VERBATIM from that page which CONTAINS the case name. Every "
    "source_url is re-fetched and checked; a case whose name is not found on "
    "its cited page is discarded, so a guess costs you the case. Never invent "
    "a case, a developer, a date, a status, or a URL. Do not cite a search "
    "engine, a news aggregator, or a real-estate listing site as the source "
    "of a case — cite the city (or county) page.\n\n"
    "LOCATION RULE: these cases are NEAR the subject, not ON it. Never "
    "describe a case as being on the subject property unless the page names "
    "the subject's own address or parcel. Put whatever the page says about "
    "where the case is into `location_text`.\n\n"
    "If the city publishes nothing usable, return an empty case list and say "
    "so plainly in coverage_note. An honest empty answer is correct; a "
    "padded one is not."
)


# ── Prompt ─────────────────────────────────────────────────────────────────


def _build_prompt(
    *,
    jurisdiction: str,
    portal_name: str | None,
    portal_url: str | None,
    address: str,
    county: str | None,
    lat: float | None,
    lng: float | None,
    radius_m: int,
    plat_clues: list[str],
) -> str:
    radius_mi = round(radius_m / 1609.34, 1)
    lines = [f"Subject site: {address}"]
    if county:
        lines.append(f"County: {county.title()} County, Texas")
    lines.append(f"Jurisdiction to search: {jurisdiction}")
    if lat is not None and lng is not None:
        lines.append(f"Coordinates: {lat:.6f}, {lng:.6f}")
    if portal_url:
        label = portal_name or "the city planning page"
        lines.append(f"The city's planning page ({label}): {portal_url}")
    if plat_clues:
        lines.append(
            "Recorded plats already known nearby (from county records) — look "
            "these up on the city's pages too, and report who is developing "
            "them if the city says: " + "; ".join(plat_clues[:10])
        )
    lines.append(
        f"\nFind development cases within about {radius_mi} miles of this "
        "site. For each, give: name, case_type (rezoning / site plan / "
        "subdivision / plat / PUD / annexation / permit), status (a SHORT "
        "phrase of at most six words, e.g. 'Approved', 'Under review', "
        "'Construction underway' — not a sentence), date, "
        "applicant_or_developer (the applicant or homebuilder named on the "
        "page, null if not stated), location_text (a SHORT phrase of at most "
        "twelve words saying where it is relative to the subject or to a "
        "named road — not a list of every street in the plat), source_url, "
        "and evidence_quote (verbatim, containing the name).\n"
        "Also return coverage_note: one or two plain sentences saying what "
        "this city actually publishes and what it does not."
    )
    return "\n".join(lines)


def _response_schema() -> dict[str, Any]:
    case = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "case_type": {"type": "string"},
            "status": {"type": "string"},
            "date": {"type": ["string", "null"]},
            "applicant_or_developer": {"type": ["string", "null"]},
            "location_text": {"type": ["string", "null"]},
            "source_url": {"type": "string"},
            "evidence_quote": {"type": "string"},
        },
        "required": list(CASE_FIELDS),
    }
    return {
        "type": "json_schema",
        "name": "city_entitlement_cases",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cases": {"type": "array", "items": case},
                "coverage_note": {"type": "string"},
            },
            "required": ["cases", "coverage_note"],
        },
    }


# ── Text extraction + the verification guard ───────────────────────────────


def _decode_entities(text: str) -> str:
    import html as _html

    return _html.unescape(text) if _ENTITY_RE.search(text) else text


def _html_to_text(markup: str) -> str:
    without_code = _TAG_RE.sub(" ", markup)
    return _WS_RE.sub(" ", _decode_entities(_MARKUP_RE.sub(" ", without_code)))


def _pdf_to_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        return _WS_RE.sub(
            " ", " ".join((page.extract_text() or "") for page in reader.pages)
        )
    except Exception as exc:
        log.info("entitlement reader: PDF text extraction failed: %s", exc)
        return ""


def _normalise(value: Any) -> str:
    """Punctuation-insensitive comparison form. 'Kerlin Place (Resub Lot 2)'
    and 'Kerlin Place — Resub, Lot 2' normalise to the same token run, so the
    guard is not defeated by a comma, but it is NOT loosened to per-word
    matching: the tokens must still appear in order and adjacent."""
    return _NON_ALNUM_RE.sub(" ", str(value or "").lower()).strip()


def _partial_name(name: str) -> str | None:
    """The head of a name, cut at the first parenthesis or comma.

    Cities routinely write 'Hutto Industrial Park' where the county plat is
    'Hutto Industrial Park, Lot 8 Replat'. Cutting at a punctuation boundary
    tolerates that WITHOUT tolerating invention: the retained head must still
    appear verbatim and be substantial (>= 2 tokens, >= 8 characters), and a
    fabricated name has no head to fall back to either."""
    head = re.split(r"[(,;–—]", name, maxsplit=1)[0]
    normalised = _normalise(head)
    if (
        len(normalised) < _MIN_PARTIAL_NAME_CHARS
        or len(normalised.split()) < _MIN_PARTIAL_NAME_TOKENS
        or normalised == _normalise(name)
    ):
        return None
    return normalised


def fetch_source_text(
    http: requests.Session,
    url: str,
    *,
    timeout_s: int,
    max_attempts: int = 1,
) -> str | None:
    """The text actually served at `url`, or None if it could not be read.

    None means UNVERIFIABLE, which means the case is dropped — an unreachable
    citation is not a citation."""
    # The orchestrator passes its shared hardened session, whose normal
    # transport raises short numeric timeouts to the global GIS floor and
    # retries at urllib3. Verification needs a much tighter, independently
    # bounded policy: use the retry-free sibling and a timeout TUPLE (which
    # `_PublicIntelSession` preserves) so this setting is the actual
    # connect/read timeout. The explicit loop is the only retry layer.
    transport = getattr(http, "no_transport_retry", http)
    attempts = max(1, int(max_attempts))
    raw = b""
    content_type = ""
    encoding = "utf-8"
    for attempt in range(1, attempts + 1):
        try:
            resp = transport.get(
                url,
                timeout=(timeout_s, timeout_s),
                allow_redirects=True,
                headers={"User-Agent": "PropDesk/1.0 entitlement-verification"},
                stream=True,
            )
            resp.raise_for_status()
            raw = resp.raw.read(_MAX_FETCH_BYTES, decode_content=True) or b""
            content_type = (resp.headers.get("Content-Type") or "").lower()
            encoding = resp.encoding or "utf-8"
            break
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status == 429 or (isinstance(status, int) and status >= 500)
            if retryable and attempt < attempts:
                continue
            log.info("entitlement reader: source fetch failed %s: %s", url, exc)
            return None
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < attempts:
                continue
            log.info("entitlement reader: source fetch failed %s: %s", url, exc)
            return None
        except Exception as exc:
            log.info("entitlement reader: source fetch failed %s: %s", url, exc)
            return None
    else:  # defensive; every unsuccessful branch above returns
        return None
    if "pdf" in content_type or raw[:5] == b"%PDF-":
        return _pdf_to_text(raw)
    try:
        markup = raw.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        markup = raw.decode("utf-8", errors="replace")
    return _html_to_text(markup)


def _match_against(case: dict[str, Any], text: str | None) -> tuple[bool, str]:
    """(verified, reason) for one case against already-retrieved page text.
    The ONLY place the pass/fail rule lives."""
    url = str(case.get("source_url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return False, "no_source_url"
    name = _normalise(case.get("name"))
    if not name:
        return False, "no_name"
    if text is None:
        return False, "source_unreachable"
    haystack = _normalise(text)
    if not haystack:
        return False, "source_empty"
    if name in haystack:
        return True, "name_matched"
    partial = _partial_name(str(case.get("name") or ""))
    if partial and partial in haystack:
        return True, "name_matched_partial"
    return False, "name_not_on_page"


def verify_case(
    http: requests.Session,
    case: dict[str, Any],
    *,
    timeout_s: int,
    max_attempts: int = 1,
) -> tuple[bool, str]:
    """(verified, reason). A case passes ONLY when its own name is found in
    the text served by its own source_url."""
    url = str(case.get("source_url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return False, "no_source_url"
    return _match_against(
        case,
        fetch_source_text(
            http, url, timeout_s=timeout_s, max_attempts=max_attempts,
        ),
    )


# ── Sanitize ───────────────────────────────────────────────────────────────


def _clean(value: Any, cap: int = _MAX_FIELD_CHARS) -> str | None:
    """Length-cap + neutralize markup: raw model output never passes through
    unsanitized (narratives.py house rule)."""
    if value is None:
        return None
    text = _WS_RE.sub(" ", _MARKUP_RE.sub(" ", str(value))).strip()
    return text[:cap] or None


def _first_sentence(value: Any, *, cap: int) -> str | None:
    """The first sentence, capped — for the fields that land in a table cell.
    Trailing detail is dropped at a clause boundary rather than mid-word, so
    the cell never reads as a truncation artefact."""
    text = _clean(value, cap=_MAX_FIELD_CHARS)
    if not text:
        return None
    head = re.split(r"(?<=[.;])\s", text, maxsplit=1)[0].rstrip(" .;")
    if len(head) <= cap:
        return head or None
    clipped = head[:cap].rsplit(" ", 1)[0].rstrip(" ,;")
    return (clipped + "…") if clipped else head[:cap]


def _sanitize_case(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = _clean(raw.get("name"))
    url = _clean(raw.get("source_url"), cap=2000)
    if not name or not url:
        return None
    return {
        "name": name,
        "case_type": _clean(raw.get("case_type"), cap=_MAX_SHORT_CHARS),
        # Status and location go into narrow table cells: a model that
        # answers with a paragraph gets its first sentence, not the card's
        # whole width.
        "status": _first_sentence(raw.get("status"), cap=_MAX_SHORT_CHARS),
        "date": _clean(raw.get("date"), cap=64),
        "applicant_or_developer": _clean(
            raw.get("applicant_or_developer"), cap=_MAX_SHORT_CHARS),
        "location_text": _first_sentence(
            raw.get("location_text"), cap=_MAX_CELL_CHARS),
        "source_url": url,
        "evidence_quote": _clean(raw.get("evidence_quote"), cap=_MAX_QUOTE_CHARS),
    }


# ── The metered read ───────────────────────────────────────────────────────


def _make_client(api_key: str) -> Any:
    """Isolated (house pattern, narratives.py) so tests can monkeypatch."""
    return OpenAI(api_key=api_key)


def _call_model(
    client: Any,
    *,
    model: str,
    prompt: str,
    max_output_tokens: int,
    max_tool_calls: int,
) -> tuple[str, int, int, int]:
    """(json_text, input_tokens, output_tokens, web_searches).

    The Responses API is the surface that carries the hosted `web_search`
    tool; its usage counters are input_tokens / output_tokens (verified
    against the live API before this module was written)."""
    resp = client.responses.create(
        model=model,
        instructions=_SYSTEM_PROMPT,
        input=prompt,
        tools=[{"type": "web_search"}],
        max_tool_calls=max_tool_calls,
        max_output_tokens=max_output_tokens,
        text={"format": _response_schema()},
    )
    searches = sum(
        1 for item in (resp.output or [])
        if getattr(item, "type", None) == "web_search_call"
    )
    usage = resp.usage
    return (
        resp.output_text or "",
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
        searches,
    )


def _release_quietly(session: Any, usage: Any) -> None:
    """Terminal `released`; never raises. No branch may leak a pending row,
    and no branch may fail the run."""
    try:
        gateway.release(session, usage)
    except Exception:
        log.exception(
            "entitlement reader release failed usage_id=%s",
            getattr(usage, "id", None),
        )
        try:
            session.rollback()
        except Exception:
            log.exception("entitlement reader rollback after failed release")


def _meta(
    *,
    status: str,
    model: str,
    error_kind: str | None = None,
    message: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"status": status, "model": model}
    if error_kind is not None:
        meta["error_kind"] = error_kind
    if message is not None:
        meta["message"] = message
    meta.update({key: value for key, value in extra.items() if value is not None})
    return meta


def read_city_cases(
    session: Any,
    run: Any,
    settings_obj: Any,
    *,
    jurisdiction: str,
    portal_name: str | None,
    portal_url: str | None,
    radius_m: int,
    plat_clues: list[str] | None = None,
    http: requests.Session | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(payload, meta) for ONE metered read plus its verification pass.

    `payload` is `{"cases": [...], "dropped_unverified": int,
    "coverage_note": str|None, ...}` — or None on any ladder branch. Every
    failure returns a plain-English message; the run never fails here.
    """
    model = str(getattr(settings_obj, "entitlement_read_model", "") or "")
    prompt = _build_prompt(
        jurisdiction=jurisdiction,
        portal_name=portal_name,
        portal_url=portal_url,
        address=str(getattr(run, "address", "") or ""),
        county=getattr(run, "county", None),
        lat=getattr(run, "lat", None),
        lng=getattr(run, "lng", None),
        radius_m=radius_m,
        plat_clues=plat_clues or [],
    )

    tenant = session.get(Tenant, run.tenant_id)
    try:
        api_key, _source = gateway.resolve_key(tenant)
    except gateway.NoTenantKey:
        return None, _meta(
            status="fallback_no_key", model=model,
            error_kind="no_key", message=MSG_NO_KEY,
        )

    price = settings_obj.openai_model_prices[model]
    max_output_tokens = int(price.max_output_tokens)
    # Conservative bytes/3 estimate of the request we actually send. Web
    # search injects page text we cannot size in advance; the gateway pads by
    # 1.25 and settle() corrects to the real counts immediately after.
    request_bytes = len(_SYSTEM_PROMPT.encode()) + len(prompt.encode())
    estimated_input_tokens = request_bytes // 3 + 1

    try:
        usage = gateway.reserve(
            session,
            tenant_id=run.tenant_id,
            user_id=getattr(run, "requested_by", None),
            app_slug=APP_SLUG,
            model=model,
            estimated_input_tokens=estimated_input_tokens,
        )
    except gateway.BudgetExceeded:
        return None, _meta(
            status="fallback_budget_cap", model=model,
            error_kind="budget_cap", message=MSG_BUDGET_CAP,
        )
    except gateway.NoTenantKey:
        return None, _meta(
            status="fallback_no_key", model=model,
            error_kind="no_key", message=MSG_NO_KEY,
        )
    usage_id = usage.id  # capture before any rollback can expire it

    client = _make_client(api_key)
    max_tool_calls = int(
        getattr(settings_obj, "entitlement_read_max_searches", 10) or 10
    )
    try:
        text, input_tokens, output_tokens, searches = _call_model(
            client, model=model, prompt=prompt,
            max_output_tokens=max_output_tokens, max_tool_calls=max_tool_calls,
        )
    except Exception as exc:
        # The call failed — nothing billable happened: release (terminal,
        # bills nothing) and degrade.
        _release_quietly(session, usage)
        status_code = getattr(exc, "status_code", None)
        log.warning(
            "entitlement read call failed run_id=%s: %s",
            getattr(run, "id", None), str(exc)[:300],
        )
        if status_code in (401, 403):
            return None, _meta(
                status="fallback_invalid_key", model=model,
                error_kind="auth", message=MSG_INVALID_KEY,
            )
        return None, _meta(
            status="fallback_provider_error", model=model,
            error_kind="provider", message=MSG_UNAVAILABLE,
        )

    # The model answered — the tokens are real, so this attempt is metered no
    # matter how the payload validates (§11.2).
    try:
        gateway.settle(session, usage, input_tokens, output_tokens)
    except Exception:
        log.exception(
            "entitlement read settlement failed run_id=%s usage_id=%s",
            getattr(run, "id", None), usage_id,
        )
        try:
            gateway.mark_settle_failed(session, usage, input_tokens, output_tokens)
        except Exception:
            log.exception(
                "entitlement read mark_settle_failed failed usage_id=%s", usage_id,
            )

    try:
        parsed = json.loads(text) if text.strip() else None
        if not isinstance(parsed, dict):
            raise TypeError("model response was not a JSON object")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        log.warning(
            "entitlement read schema-invalid run_id=%s: %s",
            getattr(run, "id", None), str(exc)[:300],
        )
        return None, _meta(
            status="fallback_invalid_output", model=model,
            error_kind="invalid_output", message=MSG_UNAVAILABLE,
            ai_call_id=usage_id,
        )

    candidates = [
        clean for clean in
        (_sanitize_case(item) for item in (parsed.get("cases") or []))
        if clean is not None
    ]
    coverage_note = _clean(parsed.get("coverage_note"), cap=600)

    # ── The guard: nothing renders without a verified citation ────────────
    verified, dropped = _verify_all(
        candidates,
        http=http_session(http),
        timeout_s=int(
            getattr(settings_obj, "entitlement_read_validation_timeout_s", 20) or 20
        ),
        max_attempts=int(
            getattr(
                settings_obj, "entitlement_read_validation_max_attempts", 2,
            ) or 2
        ),
        run_id=getattr(run, "id", None),
    )
    max_cases = int(getattr(settings_obj, "entitlement_read_max_cases", 12) or 12)

    payload = {
        "cases": verified[:max_cases],
        "verified_count": len(verified),
        "dropped_unverified": len(dropped),
        "returned_count": len(candidates),
        "coverage_note": coverage_note,
        "jurisdiction": jurisdiction,
        "portal_name": portal_name,
        "portal_url": portal_url,
        "radius_m": radius_m,
        "web_searches": searches,
    }
    if not verified:
        status = "all_dropped" if dropped else "no_cases_found"
        return payload, _meta(
            status=status, model=model, ai_call_id=usage_id,
            message=MSG_ALL_DROPPED if dropped else MSG_NONE_FOUND,
            dropped_unverified=len(dropped), web_searches=searches,
        )
    return payload, _meta(
        status="ok", model=model, ai_call_id=usage_id,
        verified_count=len(verified), dropped_unverified=len(dropped),
        web_searches=searches,
    )


def _verify_all(
    candidates: list[dict[str, Any]],
    *,
    http: requests.Session,
    timeout_s: int,
    max_attempts: int,
    run_id: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(verified, dropped). One fetch per distinct URL — a city listing page
    usually carries several cases, and re-downloading it per case would be
    both slow and rude to the city's server."""
    text_by_url: dict[str, str | None] = {}
    verified: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for case in candidates:
        url = str(case.get("source_url") or "").strip()
        if url and url not in text_by_url:
            text_by_url[url] = (
                fetch_source_text(
                    http,
                    url,
                    timeout_s=timeout_s,
                    max_attempts=max_attempts,
                )
                if url.lower().startswith(("http://", "https://")) else None
            )
        ok, reason = _match_against(case, text_by_url.get(url))
        if ok:
            verified.append({**case, "verification": reason})
        else:
            dropped.append({**case, "drop_reason": reason})
            log.warning(
                "entitlement read DROPPED unverified case run_id=%s "
                "reason=%s name=%r url=%s",
                run_id, reason, case.get("name"), case.get("source_url"),
            )
    return verified, dropped


# ── Orchestrator-facing builder ────────────────────────────────────────────


def _scope_key(run: Any, jurisdiction: str, radius_m: int) -> str:
    """Cache scope: the jurisdiction, the radius, and the site. Rounded to
    ~10 m so two lookups of the same corner share a read."""
    lat, lng = getattr(run, "lat", None), getattr(run, "lng", None)
    anchor = (
        f"{float(lat):.4f},{float(lng):.4f}"
        if lat is not None and lng is not None else "no-anchor"
    )
    return f"{jurisdiction.lower()}|r={radius_m}|{anchor}"


def _plat_clues(facts: dict[str, Any]) -> list[str]:
    """Names of the recorded plats the deterministic sweep already found —
    handed to the model so it can look up who is developing them."""
    clues: list[str] = []
    for case in facts.get("subdivision_cases") or []:
        if isinstance(case, dict):
            name = str(case.get("name") or "").strip()
            if name:
                clues.append(name)
    return clues


def build_entitlement_reads(
    session: Any,
    run: Any,
    results: dict[str, Any],
    settings_obj: Any,
    *,
    http: requests.Session | None = None,
) -> dict[str, Any]:
    """Post-staging enrichment (mirrors build_historical_aerials). ALWAYS
    returns a meta dict and NEVER raises: the deterministic county plat
    records on this card must render whatever happens here.

    On success it merges `city_case_reads` into the entitlement card's facts
    in `results`; the caller reassigns `run.adapter_results`.
    """
    model = str(getattr(settings_obj, "entitlement_read_model", "") or "")
    if not getattr(settings_obj, "entitlement_read_enabled", True):
        return {"entitlement_read_meta": {"status": "disabled", "model": model}}
    try:
        return _build(session, run, results, settings_obj, http=http)
    except Exception:
        # Absolute boundary — an unexpected bug degrades, never fails the run.
        log.exception(
            "entitlement reader unexpected failure run_id=%s",
            getattr(run, "id", None),
        )
        return {
            "entitlement_read_meta": _meta(
                status="fallback_provider_error", model=model,
                error_kind="provider", message=MSG_UNAVAILABLE,
            ),
        }


def _build(
    session: Any,
    run: Any,
    results: dict[str, Any],
    settings_obj: Any,
    *,
    http: requests.Session | None = None,
) -> dict[str, Any]:
    model = str(getattr(settings_obj, "entitlement_read_model", "") or "")
    result = results.get(ENTITLEMENT_SOURCE_ID)
    facts = result.get("normalized_facts") if isinstance(result, dict) else None
    if not isinstance(facts, dict):
        return {"entitlement_read_meta": {"status": "no_entitlement_result",
                                          "model": model}}

    jurisdiction = str(facts.get("jurisdiction") or "").strip()
    portal_url = str(facts.get("case_portal_url") or "").strip() or None
    portal_name = str(facts.get("case_portal_name") or "").strip() or None
    if not jurisdiction or not portal_url:
        # Nothing to read: no city identified, or no page on file for it.
        return {
            "entitlement_read_meta": _meta(
                status="skipped_no_portal", model=model, message=MSG_NO_PORTAL,
            ),
        }

    radius_m = int(getattr(settings_obj, "entitlement_read_radius_m", 3200) or 3200)
    force_refresh = str(getattr(run, "mode", "") or "") == "fresh"

    # The live pass reports through this holder, not through the payload:
    # `_meta` carries the ai_call_id and the ladder status of THIS run, which
    # must never be written into the shared cache row and replayed at a later
    # reader as if it were their call.
    live: dict[str, Any] = {}

    def _fetch() -> dict[str, Any]:
        payload, meta = read_city_cases(
            session, run, settings_obj,
            jurisdiction=jurisdiction,
            portal_name=portal_name,
            portal_url=portal_url,
            radius_m=radius_m,
            plat_clues=_plat_clues(facts),
            http=http,
        )
        live["meta"] = meta
        live["payload"] = payload
        if payload is None or not payload.get("cases"):
            # Not cacheable — a ladder failure or an empty read must be
            # retried next time, not frozen for a week. (The cache helper
            # refuses empty-fact payloads anyway; being explicit documents
            # the intent.)
            return {"normalized_facts": {}}
        return {"normalized_facts": {"read": payload}}

    try:
        outcome = source_cache.get_or_fetch(
            session,
            source_id=SOURCE_ID,
            scope_key=_scope_key(run, jurisdiction, radius_m),
            producer_version=PRODUCER_VERSION,
            fetch=_fetch,
            ttl_days=int(
                getattr(settings_obj, "entitlement_read_cache_ttl_days", 7) or 7
            ),
            force_refresh=force_refresh,
        )
    except Exception:
        log.warning(
            "entitlement read cache layer failed run_id=%s",
            getattr(run, "id", None), exc_info=True,
        )
        return {
            "entitlement_read_meta": _meta(
                status="fallback_provider_error", model=model,
                error_kind="provider", message=MSG_UNAVAILABLE,
            ),
        }

    read = ((outcome.get("payload") or {}).get("normalized_facts") or {}).get("read")
    meta = live.get("meta")
    if not isinstance(meta, dict):
        # No live pass ran — this came off a stored row, so the status
        # describes the STORED read, not a call that did not happen.
        meta = _meta(
            status="ok" if read else "no_cases_found", model=model,
            message=None if read else MSG_NONE_FOUND,
        )
        if isinstance(read, dict):
            meta["verified_count"] = len(read.get("cases") or [])
            meta["dropped_unverified"] = read.get("dropped_unverified")
    if not read and isinstance(live.get("payload"), dict):
        # A live read that verified nothing is not cacheable, so the outcome
        # payload is empty — but the honest counts still belong on the card.
        read = live["payload"]
    cache_state = (
        "stale" if outcome.get("stale")
        else ("hit" if outcome.get("from_cache") else "miss")
    )
    meta = {**meta, "cache": cache_state}

    if isinstance(read, dict) and read.get("cases"):
        facts["city_case_reads"] = read
        return {"entitlement_read_meta": meta, "entitlement_read_applied": True}
    if isinstance(read, dict):
        # A read that verified nothing still tells the reader the pages WERE
        # checked — that is materially different from never looking.
        facts["city_case_reads"] = {**read, "cases": []}
    return {"entitlement_read_meta": meta}


__all__ = [
    "APP_SLUG",
    "CASE_FIELDS",
    "MSG_ALL_DROPPED",
    "MSG_NONE_FOUND",
    "MSG_NO_PORTAL",
    "PRODUCER_VERSION",
    "SOURCE_ID",
    "build_entitlement_reads",
    "fetch_source_text",
    "read_city_cases",
    "verify_case",
]
