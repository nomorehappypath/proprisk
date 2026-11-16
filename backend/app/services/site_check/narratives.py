"""OpenAI narrative overlay for Public Data cards (site_check.md §11).

One model call per run (plus at most ONE repair retry iff the first response
was schema-invalid) through the brick-10 gateway: reserve → call → settle.
Every attempted call gets its own reservation and its own ai_usage row. The
deterministic adapter JSON stays the source of truth — this module only
rewrites already-fetched facts into broker-readable card guidance.

The run NEVER fails because of anything in here: every branch of the §11.3
degradation ladder returns a result dict (empty narratives + a plain-English
message) and settles or releases its reservation — no pending row leaks.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import OpenAI

from app.models import Tenant
from app.services import gateway

log = logging.getLogger("propdesk.site_check.narratives")

# ── Ported compaction bounds (pinned card_narratives.py) ───────────────────

_EXCLUDED_SOURCES = {"official_records"}
_MAX_SOURCES = 24
_MAX_DICT_KEYS = 28
_MAX_LIST_ITEMS = 6
_MAX_STRING_CHARS = 700
_MAX_DEPTH = 4
_STRIPPED_KEYS = {"raw_records", "raw_payload", "geometry", "parcel_polygon"}

_NARRATIVE_FIELDS = (
    "headline", "broker_read", "why_it_matters", "next_step", "caveat",
)
_MAX_FIELD_CHARS = 900

APP_SLUG = "site-check"

# §11.3 plain-English ladder messages (shared strings — the background job
# never pretends an HTTP 402 happened).
MSG_NO_KEY = "Add your OpenAI key in Settings to get written summaries."
MSG_BUDGET_CAP = "This month's AI budget is used up. It resets on the 1st."
MSG_INVALID_KEY = "Your OpenAI key was rejected — check it in Settings."
MSG_UNAVAILABLE = (
    "Summaries are unavailable right now — the facts below are complete."
)

# Ported honesty rules (pinned system prompt), reworded for PropDesk.
_SYSTEM_PROMPT = (
    "You write short card summaries for PropDesk Public Data, a tool small "
    "real-estate investors use to decide whether a site is worth more of "
    "their time. The JSON provided is the source of truth. Do not invent "
    "facts, URLs, distances, document numbers, laws, statuses, or "
    "requirements. A field describing a feature's own size or length (for "
    "example a stream segment's length in km) is NOT its distance from the "
    "property — never restate it as proximity. State how far something is "
    "from the site only when the JSON gives an explicit distance; when only "
    "a search radius is provided, say 'within N m', never a specific "
    "distance. For each source card, write plain language a non-engineer "
    "understands: what it means, why it matters, and the next practical "
    "action if any. Collapse repetitive rows into one useful signal. Avoid "
    "engineer-speak and raw table narration. If a source was not actually "
    "checked, say it was not checked — never say zero were found. For the "
    "austin_zoning card, do not describe a verified jurisdiction plus a "
    "missing base zone as an overall unknown; say the jurisdiction is "
    "verified and the base district is not published or still needs a "
    "zoning letter. If zoning_read_status is use_action_only, mention the "
    "use action without implying the base zoning district or final approval "
    "status was proven. For the environmental_screening card, no records is "
    "a LIMITED screen of a few public databases (petroleum storage tanks "
    "and federally-regulated facilities) — it does NOT establish that the "
    "site is clean. Never say there is 'no contamination' or that the site "
    "is environmentally clear/clean. Say only that the screened public "
    "sources returned no records within the search radius, that historical "
    "site use and state cleanup/complaint records are NOT assessed by this "
    "screen, and that a Phase I ESA is required to establish the site's "
    "environmental condition. When a card's JSON contains material_findings "
    "entries with severity 'high', those are CONFIRMED deal-shaping "
    "constraints on official map layers: the card MUST lead with the "
    "finding and its concrete consequence. Use direct language a buyer "
    "cannot miss — never soften a confirmed high-severity finding to "
    "'specialist review required' or similar boilerplate. Return JSON "
    "matching the requested schema only."
)

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


# ── Compaction (ported) ────────────────────────────────────────────────────


def _scalar_preview(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_MAX_STRING_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STRING_CHARS]


def _compact_value(value: Any, depth: int = 0) -> Any:
    if depth >= _MAX_DEPTH:
        return _scalar_preview(value)
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= _MAX_DICT_KEYS:
                compact["_truncated_keys"] = len(value) - _MAX_DICT_KEYS
                break
            if key in _STRIPPED_KEYS:
                continue
            compact[str(key)] = _compact_value(item, depth + 1)
        return compact
    if isinstance(value, list):
        items = [_compact_value(item, depth + 1) for item in value[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            items.append({"_truncated_items": len(value) - _MAX_LIST_ITEMS})
        return items
    return _scalar_preview(value)


def _card_payload(adapter_results: dict[str, Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for source_id, result in (adapter_results or {}).items():
        if source_id in _EXCLUDED_SOURCES:
            continue
        result = result if isinstance(result, dict) else {}
        facts = result.get("normalized_facts") or {}
        coverage = result.get("coverage_status")
        errors = result.get("errors") or []
        if not facts and coverage in {
            "not_yet_wired", "not_supported_in_jurisdiction",
        }:
            continue
        cards.append({
            "source_id": str(source_id),
            "coverage_status": coverage,
            "data_confidence_status": result.get("data_confidence_status"),
            "errors": _compact_value(errors),
            "facts": _compact_value(facts),
        })
    return cards[:_MAX_SOURCES]


# ── Prompt + strict schema ─────────────────────────────────────────────────


def _build_user_prompt(run: Any, cards: list[dict[str, Any]]) -> str:
    context = {
        "address": getattr(run, "address", None),
        "county": getattr(run, "county", None),
        "parcel_id": getattr(run, "parcel_id", None),
    }
    return (
        "SITE:\n" + json.dumps(context, ensure_ascii=False)
        + "\n\nPUBLIC DATA CARDS:\n" + json.dumps(cards, ensure_ascii=False)
        + "\n\nReturn a JSON object with one entry per source_id plus a "
        "top-level \"bottom_line\". Each source entry must be:\n"
        "{"
        '"headline": "short card headline", '
        '"broker_read": "1-2 plain-English sentences", '
        '"why_it_matters": "one sentence", '
        '"next_step": "action or null", '
        '"caveat": "screening caveat or null"'
        "}\n"
        "bottom_line: one or two plain sentences answering the only question "
        "that matters — is this site worth more of the reader's time? Lead "
        "with the biggest confirmed constraint, or the all-clear.\n"
        "Rules: use only supplied facts; if a source was not actually "
        "checked, say not checked, not zero found; deduplicate repeated "
        "facilities, flowlines, places, schools, and project rows."
    )


def _response_schema(source_ids: list[str]) -> dict[str, Any]:
    """Strict JSON schema — per-source five fields plus the top-level
    bottom_line: the COMPLETE allowlist (§11.2/§10.6)."""
    entry = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            field: {"type": ["string", "null"]} for field in _NARRATIVE_FIELDS
        },
        "required": list(_NARRATIVE_FIELDS),
    }
    properties: dict[str, Any] = {sid: entry for sid in source_ids}
    properties["bottom_line"] = {"type": "string"}
    return {
        "name": "site_check_narratives",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties),
        },
    }


# ── Call + validation + sanitize ───────────────────────────────────────────


def _make_client(api_key: str) -> Any:
    """Isolated (house pattern, extraction.py) so tests can monkeypatch."""
    return OpenAI(api_key=api_key)


def _call_model(
    client: Any, *, model: str, user_prompt: str,
    schema: dict[str, Any], max_output_tokens: int,
) -> tuple[str, int, int]:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_completion_tokens=max_output_tokens,
        response_format={"type": "json_schema", "json_schema": schema},
    )
    return (
        resp.choices[0].message.content or "",
        resp.usage.prompt_tokens,
        resp.usage.completion_tokens,
    )


def _parse_and_validate(text: str, source_ids: list[str]) -> dict[str, Any]:
    """Raise ValueError on anything schema-invalid (triggers the one repair
    retry). Extra keys are NOT an error here — the sanitizer strips them."""
    if not text.strip():
        raise ValueError("empty model response")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model response was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise TypeError("model response was not a JSON object")
    bottom_line = parsed.get("bottom_line")
    if not isinstance(bottom_line, str) or not bottom_line.strip():
        raise ValueError("model response missing bottom_line")
    for key, value in parsed.items():
        if key == "bottom_line":
            continue
        if key in source_ids and not isinstance(value, dict):
            raise ValueError(f"narrative entry for {key!r} is not an object")
    return parsed


def _clean_text(value: Any, cap: int = _MAX_FIELD_CHARS) -> str | None:
    """Length-cap + neutralize markup/injection: tags stripped, whitespace
    collapsed. Raw model output never passes through unsanitized."""
    if value is None:
        return None
    text = _TAG_RE.sub(" ", str(value))
    text = _WS_RE.sub(" ", text).strip()
    return text[:cap] or None


def _sanitize(
    parsed: dict[str, Any], source_ids: list[str],
) -> tuple[dict[str, dict[str, str | None]], str | None]:
    """Keep ONLY the allowlisted shape: known source_ids, the five narrative
    fields, and bottom_line — everything else is stripped."""
    narratives: dict[str, dict[str, str | None]] = {}
    for source_id in source_ids:
        entry = parsed.get(source_id)
        if not isinstance(entry, dict):
            continue
        cleaned = {
            field: _clean_text(entry.get(field)) for field in _NARRATIVE_FIELDS
        }
        if cleaned["headline"] or cleaned["broker_read"]:
            narratives[source_id] = cleaned
    bottom_line = _clean_text(parsed.get("bottom_line"))
    return narratives, bottom_line


def _classify_call_error(exc: Exception) -> str:
    """auth (invalid/revoked key) vs provider (timeout / 5xx / network /
    rate limit / anything else)."""
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "auth"
    return "provider"


# ── Result assembly ────────────────────────────────────────────────────────


def _result(
    *,
    status: str,
    model: str,
    attempts: int,
    narratives: dict[str, Any] | None = None,
    bottom_line: str | None = None,
    ai_call_id: Any = None,
    error_kind: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "status": status,
        "model": model,
        "attempts": attempts,
    }
    if ai_call_id is not None:
        meta["ai_call_id"] = ai_call_id
    if error_kind is not None:
        meta["error_kind"] = error_kind
    if message is not None:
        meta["message"] = message
    return {
        "narratives": narratives or {},
        "narrative_bottom_line": bottom_line,
        "narrative_meta": meta,
    }


def build_narratives(
    session: Any, run: Any, adapter_results: dict[str, Any], settings: Any,
) -> dict[str, Any]:
    """§11.2/§11.3 entry point. Always returns a result dict — the run never
    fails from anything in this module."""
    model = settings.site_check_narrative_model
    try:
        return _build(session, run, adapter_results, settings, model)
    except Exception:
        # Absolute boundary — an unexpected bug degrades, never fails the run.
        log.exception(
            "site_check narratives unexpected failure run_id=%s",
            getattr(run, "id", None),
        )
        return _result(
            status="fallback_provider_error", model=model, attempts=0,
            error_kind="provider", message=MSG_UNAVAILABLE,
        )


def _build(
    session: Any, run: Any, adapter_results: dict[str, Any],
    settings: Any, model: str,
) -> dict[str, Any]:
    cards = _card_payload(adapter_results)
    if not cards:
        return _result(status="skipped_no_facts", model=model, attempts=0)

    tenant = session.get(Tenant, run.tenant_id)
    try:
        api_key, _source = gateway.resolve_key(tenant)
    except gateway.NoTenantKey:
        return _result(
            status="fallback_no_key", model=model, attempts=0,
            error_kind="no_key", message=MSG_NO_KEY,
        )

    source_ids = [card["source_id"] for card in cards]
    user_prompt = _build_user_prompt(run, cards)
    schema = _response_schema(source_ids)
    price = settings.openai_model_prices[model]
    max_output_tokens = int(price.max_output_tokens)
    # Conservative bytes/3 input estimate from the actual serialized request
    # (§11.2) — the reservation formula pads it by 1.25 in the gateway.
    request_bytes = len(_SYSTEM_PROMPT.encode()) + len(user_prompt.encode())
    estimated_input_tokens = request_bytes // 3 + 1

    client = _make_client(api_key)
    attempts = 0
    # At most TWO calls: the initial one plus ONE repair retry iff the first
    # returned schema-invalid output. Each gets its own reservation + row.
    while attempts < 2:
        attempts += 1
        try:
            usage = gateway.reserve(
                session,
                tenant_id=run.tenant_id,
                user_id=run.requested_by,
                app_slug=APP_SLUG,
                model=model,
                estimated_input_tokens=estimated_input_tokens,
            )
        except gateway.BudgetExceeded:
            return _result(
                status="fallback_budget_cap", model=model,
                attempts=attempts - 1, error_kind="budget_cap",
                message=MSG_BUDGET_CAP,
            )
        except gateway.NoTenantKey:
            return _result(
                status="fallback_no_key", model=model, attempts=attempts - 1,
                error_kind="no_key", message=MSG_NO_KEY,
            )
        usage_id = usage.id  # capture before any rollback can expire it

        try:
            text, prompt_tokens, completion_tokens = _call_model(
                client, model=model, user_prompt=user_prompt,
                schema=schema, max_output_tokens=max_output_tokens,
            )
        except Exception as exc:
            # The call itself failed — nothing billable happened: release the
            # reservation (terminal, bills nothing) and degrade.
            _release_quietly(session, usage)
            kind = _classify_call_error(exc)
            log.warning(
                "site_check narrative call failed run_id=%s kind=%s: %s",
                getattr(run, "id", None), kind, str(exc)[:300],
            )
            if kind == "auth":
                return _result(
                    status="fallback_invalid_key", model=model,
                    attempts=attempts, error_kind="auth",
                    message=MSG_INVALID_KEY,
                )
            return _result(
                status="fallback_provider_error", model=model,
                attempts=attempts, error_kind="provider",
                message=MSG_UNAVAILABLE,
            )

        # The model answered — the tokens are real, so this attempt is
        # metered no matter how the payload validates (§11.2). A settlement
        # error becomes the durable settle_failed repair state (the narrative
        # is kept; the reaper finishes the metering).
        try:
            gateway.settle(session, usage, prompt_tokens, completion_tokens)
        except Exception:
            log.exception(
                "site_check narrative settlement failed run_id=%s usage_id=%s",
                getattr(run, "id", None), usage_id,
            )
            try:
                gateway.mark_settle_failed(
                    session, usage, prompt_tokens, completion_tokens,
                )
            except Exception:
                log.exception(
                    "site_check narrative mark_settle_failed failed "
                    "usage_id=%s", usage_id,
                )

        try:
            parsed = _parse_and_validate(text, source_ids)
        except (TypeError, ValueError) as exc:
            log.warning(
                "site_check narrative schema-invalid attempt=%d run_id=%s: %s",
                attempts, getattr(run, "id", None), str(exc)[:300],
            )
            if attempts >= 2:
                return _result(
                    status="fallback_invalid_output", model=model,
                    attempts=attempts, error_kind="invalid_output",
                    message=MSG_UNAVAILABLE,
                )
            continue  # ONE repair retry, with its own reservation + row

        narratives, bottom_line = _sanitize(parsed, source_ids)
        return _result(
            status="ok", model=model, attempts=attempts,
            narratives=narratives, bottom_line=bottom_line,
            ai_call_id=usage_id,
        )

    # Unreachable — the loop always returns — kept as a safety net.
    return _result(
        status="fallback_invalid_output", model=model, attempts=attempts,
        error_kind="invalid_output", message=MSG_UNAVAILABLE,
    )


def _release_quietly(session: Any, usage: Any) -> None:
    """Terminal `released` on the reservation; never raises (no branch may
    leak a pending row, and no branch may fail the run)."""
    try:
        gateway.release(session, usage)
    except Exception:
        log.exception(
            "site_check narrative release failed usage_id=%s",
            getattr(usage, "id", None),
        )
        try:
            session.rollback()
        except Exception:
            log.exception("site_check narrative rollback after failed release")


__all__ = [
    "APP_SLUG",
    "MSG_BUDGET_CAP",
    "MSG_INVALID_KEY",
    "MSG_NO_KEY",
    "MSG_UNAVAILABLE",
    "build_narratives",
]
