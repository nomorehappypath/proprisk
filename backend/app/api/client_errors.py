"""Receives one browser error report from `web/src/lib/errorReporter.ts` and
hands it to the central error sink (SPEC.md chapter 8). Always answers 204:
a failing report must never itself fail the page that sent it, except for
an oversized body, which is rejected before it is read in full.
"""

from fastapi import APIRouter, Request

from app.api.deps import plain_error
from app.core.error_sink import report_error

router = APIRouter(tags=["client-errors"])

MAX_BODY_BYTES = 16 * 1024


@router.post("/client-errors", status_code=204)
async def post_client_error(request: Request) -> None:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise plain_error(422, "That error report is too large.")
    try:
        import json

        payload = json.loads(body) if body else {}
    except ValueError:
        payload = {"raw": body.decode("utf-8", "replace")}
    message = str(payload.get("message", "browser error"))
    kind = str(payload.get("kind")) if payload.get("kind") else None
    report_error(message, where="browser", context=payload, source="browser", kind=kind)
