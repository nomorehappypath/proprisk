"""Free HIFLD electric transmission and substation proximity screen."""

from __future__ import annotations

import requests

from app.services.site_check.adapters._base import AdapterResult, PropertyIdentity
from app.services.site_check.hifld_power import fetch_hifld_power_paths

SOURCE_ID = "electric_power_infrastructure"


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    payload = fetch_hifld_power_paths(
        lat=identity.lat,
        lng=identity.lng,
        session=session,
    )
    payload["source_id"] = SOURCE_ID
    return AdapterResult.model_validate(payload)


__all__ = ["SOURCE_ID", "fetch"]
