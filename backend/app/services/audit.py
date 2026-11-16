"""Audit spine (objective.md critical rule 8): one row per mutation, same transaction.

Callers add the row via `log(...)` and COMMIT THEMSELVES — the row rides the
mutation's transaction, so a failed mutation leaves no audit row. `detail`
carries only allowlisted field diffs; the MICR rule applies transitively
because values come from already-scrubbed records.
"""

from sqlalchemy.orm import Session

from app.models import AuditLog


def diff(before: dict, after: dict) -> dict:
    """Field-level {"changed": {field: {"from", "to"}}} over the given snapshots."""
    changed = {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in after
        if before.get(key) != after.get(key)
    }
    return {"changed": changed}


def log(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    detail: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail,
        )
    )
