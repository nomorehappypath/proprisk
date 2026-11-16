"""The Settings menu's API: what PropRisk needs, the keys and secrets store,
this month's AI spending, and where PropRisk has local coverage. Replaces
PropDesk's `api/platform.py` AI-key and usage-summary routes with ones that
read and write the local secret store instead of a per-tenant vault.
"""

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_tenant, current_user, plain_error
from app.config.settings import settings
from app.core import secret_store
from app.db import get_db
from app.models import SiteCheckProviderUsage, SiteCheckRun, Tenant, User
from app.services import audit, gateway
from app.services.site_check.coverage import get_coverage

router = APIRouter(prefix="/settings", tags=["settings"])

KEY_SHAPE_ERROR = "Please check the key — it should be 8 to 512 characters with no spaces."
KEY_CHECK_FAILED = "That key didn't work — check it and try again."

# One entry per name in SPEC.md chapter 1.6, used to build the "What PropRisk
# needs" checklist and to label each key panel.
KEY_INFO: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "required": "optional",
        "without": (
            "No AI summaries; aerial gallery shows photos without the AI "
            "reading; no entitlement reading"
        ),
        "signup_url": "https://platform.openai.com/api-keys",
        "cost": (
            "Paid per use (prepaid credit on the OpenAI account; PropRisk "
            "stops AI calls at the monthly cap, $20.00 by default)"
        ),
    },
    "census": {
        "label": "Census",
        "required": "recommended",
        "without": "Demographics card is empty",
        "signup_url": "https://api.census.gov/data/key_signup.html",
        "cost": "Free",
    },
    "arcgis": {
        "label": "ArcGIS",
        "required": "optional",
        "without": "Address finding uses OpenStreetMap, then Google if set; places fallback off",
        "signup_url": "https://developers.arcgis.com/sign-up/",
        "cost": (
            "Free monthly allowance; past it the service stops unless "
            "pay-as-you-go is switched on"
        ),
    },
    "google_maps": {
        "label": "Google Maps",
        "required": "optional",
        "without": "Last fallback skipped; an address no tier finds fails the lookup",
        "signup_url": "https://console.cloud.google.com/google/maps-apis",
        "cost": "Paid per call after a free monthly allowance; a billing account is required",
    },
    "brave_search": {
        "label": "Brave Search",
        "required": "optional",
        "without": (
            "Plat search uses an old Bing Search key if one is set, else the "
            "free web-page search"
        ),
        "signup_url": "https://api-dashboard.search.brave.com",
        "cost": "Paid per search (metered billing with a small monthly credit; a card is required)",
    },
    "bing_search": {
        "label": "Bing Search — retired by Microsoft (August 2025); use Brave Search instead",
        "required": "optional",
        "without": "Plat search uses a Brave key if set, else the free web-page search",
        "signup_url": "https://api-dashboard.search.brave.com",
        "cost": "Retired — no new keys; an existing key from an older agreement keeps working",
    },
    "error_sink_token": {
        "label": "Error forwarding token",
        "required": "optional",
        "without": "Errors stay in the local file only",
        "signup_url": None,
        "cost": "Not a provider key — free on PropRisk's side",
    },
    "contact_email": {
        "label": "Contact email",
        "required": "optional",
        "without": "Outbound requests to public services identify PropRisk with no contact address",
        "signup_url": None,
        "cost": "Free — this is your own address",
    },
}

MAC_PREREQUISITES = [
    {
        "name": "postgresql",
        "label": "PostgreSQL 14 or newer",
        "install_hint": "brew install postgresql@14",
    },
    {
        "name": "python_uv",
        "label": "Python 3.12 with uv",
        "install_hint": "brew install uv",
    },
    {
        "name": "node",
        "label": "Node 20 or newer",
        "install_hint": "brew install node",
    },
]


@router.get("/needs")
def get_needs() -> dict:
    secrets_by_name = {row["name"]: row for row in secret_store.list_secrets()}
    rows = [
        {
            "name": item["name"],
            "label": item["label"],
            "group": "mac",
            "required": "required",
            "without": "PropRisk cannot launch without it",
            "signup_url": None,
            "cost": "Free",
            "is_set": None,
            "last4": None,
            "updated_at": None,
            "install_hint": item["install_hint"],
        }
        for item in MAC_PREREQUISITES
    ]
    for name, info in KEY_INFO.items():
        found = secrets_by_name.get(name, {"is_set": False, "last4": None, "updated_at": None})
        rows.append(
            {
                "name": name,
                "label": info["label"],
                "group": "key",
                "required": info["required"],
                "without": info["without"],
                "signup_url": info["signup_url"],
                "cost": info["cost"],
                "is_set": found["is_set"],
                "last4": found["last4"],
                "updated_at": found["updated_at"],
                "install_hint": None,
            }
        )
    return {"rows": rows}


@router.get("/keys")
def get_keys() -> list[dict]:
    return [
        {
            "name": row["name"],
            "label": KEY_INFO.get(row["name"], {}).get("label", row["name"]),
            "is_set": row["is_set"],
            "last4": row["last4"],
            "updated_at": row["updated_at"],
        }
        for row in secret_store.list_secrets()
    ]


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SetKeyBody(BaseModel):
    value: str = Field(min_length=1, max_length=512)


def validate_openai_key(key: str) -> bool:
    """Minimal live probe; isolated so tests can monkeypatch it."""
    from openai import OpenAI

    try:
        OpenAI(api_key=key).models.list()
        return True
    except Exception:
        return False


@router.put("/keys/{name}", status_code=204)
def set_key(
    name: str,
    body: SetKeyBody,
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> None:
    if name not in secret_store.ALLOWED_NAMES:
        raise plain_error(404, "We couldn't find that.")
    value = body.value

    if name == "contact_email":
        if len(value) > 254 or not _EMAIL_SHAPE.match(value):
            raise plain_error(422, "Please check the email address.")
    else:
        if not (8 <= len(value) <= 512) or " " in value or _CONTROL_CHARS.search(value):
            raise plain_error(422, KEY_SHAPE_ERROR)
        if name == "openai" and not validate_openai_key(value):
            raise plain_error(422, KEY_CHECK_FAILED)

    secret_store.set_secret(name, value)
    audit.log(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        action="secret.set",
        entity_type="secret",
        entity_id=name,
        detail={"last4": value[-4:] if len(value) >= 4 else value},
    )
    db.commit()


@router.delete("/keys/{name}", status_code=204)
def delete_key(
    name: str,
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> None:
    if name not in secret_store.ALLOWED_NAMES:
        raise plain_error(404, "We couldn't find that.")
    secret_store.delete_secret(name)
    audit.log(
        db, tenant_id=tenant.id, user_id=user.id,
        action="secret.delete", entity_type="secret", entity_id=name, detail=None,
    )
    db.commit()


class SpendingCapBody(BaseModel):
    budget_cents: int = Field(ge=0, le=1_000_000)


def compute_spending(db: Session, tenant: Tenant) -> dict:
    """The body of GET /spending, callable directly from tests against any
    office row — not only the fixed local one — so provider-usage
    aggregation stays checked at the service layer (PropRisk has only one
    HTTP identity, so a second office can't be reached over HTTP)."""
    now = datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)

    spend_cents = gateway.month_spend_cents(db, tenant.id, now)
    budget_cents = gateway.budget_cap_cents(tenant)

    lookups = int(
        db.execute(
            select(func.count()).select_from(SiteCheckRun).where(
                SiteCheckRun.tenant_id == tenant.id,
                SiteCheckRun.created_at >= start,
                SiteCheckRun.created_at < end,
            )
        ).scalar_one()
    )
    provider_rows = db.execute(
        select(
            SiteCheckProviderUsage.provider,
            func.sum(SiteCheckProviderUsage.calls),
            func.sum(SiteCheckProviderUsage.estimated_cost_microdollars),
        )
        .where(
            SiteCheckProviderUsage.tenant_id == tenant.id,
            SiteCheckProviderUsage.created_at >= start,
            SiteCheckProviderUsage.created_at < end,
        )
        .group_by(SiteCheckProviderUsage.provider)
        .order_by(SiteCheckProviderUsage.provider)
    ).all()
    source_calls = sum(int(calls) for _, calls, _ in provider_rows)
    total_micro = sum(int(micro) for _, _, micro in provider_rows)

    return {
        "month": start.strftime("%Y-%m"),
        "spend_cents": spend_cents,
        "budget_cents": budget_cents,
        "warn": spend_cents >= budget_cents * settings.budget_warn_threshold,
        "blocked": spend_cents + settings.ai_reserve_cents_per_call > budget_cents,
        "public_data": {
            "lookups": lookups,
            "source_calls": source_calls,
            "estimated_paid_cost_cents": -(-total_micro // 10_000),  # ceiling µ$ → cents
            "by_source": [
                {
                    "source": provider,
                    "calls": int(calls),
                    "estimated_cost_cents": -(-int(micro) // 10_000),
                }
                for provider, calls, micro in provider_rows
            ],
        },
    }


@router.get("/spending")
def get_spending(
    tenant: Tenant = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> dict:
    return compute_spending(db, tenant)


@router.put("/spending/cap", status_code=204)
def set_spending_cap(
    body: SpendingCapBody,
    tenant: Tenant = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> None:
    tenant.monthly_budget_cents = body.budget_cents
    db.commit()


@router.get("/coverage")
def coverage() -> dict:
    return get_coverage()
