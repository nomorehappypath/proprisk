"""PropRisk's server: mounts the kept Public Data API, the new Settings and
client-error APIs, serves the built screens (`web/dist`) for every
non-`/api` path, and protects the local-only app from a foreign web page
trying to reach it (SPEC.md chapter 7)."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.client_errors import router as client_errors_router
from app.api.settings import router as settings_router
from app.api.site_check import router as site_check_router
from app.config.settings import settings
from app.core.error_sink import install as install_error_sink
from app.core.error_sink import report_error

install_error_sink()

app = FastAPI(title=settings.app_name)

LOCAL_HOSTNAMES = {"127.0.0.1", "localhost"}
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _hostname(value: str) -> str:
    return value.split(":", 1)[0].lower()


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    """Any web page open in the same browser could try to call
    127.0.0.1:<port>, so every request's Host must name this machine, and a
    state-changing request's Origin (when sent) must be this app's own."""

    async def dispatch(self, request: Request, call_next):
        host_header = request.headers.get("host", "")
        if _hostname(host_header) not in LOCAL_HOSTNAMES:
            return JSONResponse(status_code=400, content={"error": "Please check the address — try again."})

        if request.method in STATE_CHANGING_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                from urllib.parse import urlparse

                if _hostname(urlparse(origin).netloc) not in LOCAL_HOSTNAMES:
                    return JSONResponse(
                        status_code=403,
                        content={"error": "This request did not come from PropRisk itself."},
                    )
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > 0:
                content_type = request.headers.get("content-type", "")
                if not content_type.startswith("application/json"):
                    return JSONResponse(
                        status_code=415,
                        content={"error": "Please send this request as JSON."},
                    )
        return await call_next(request)


app.add_middleware(LocalOnlyMiddleware)


@app.exception_handler(HTTPException)
async def plain_error_body(request: Request, exc: HTTPException) -> JSONResponse:
    """Contract: every non-2xx body is {"error": "<plain-language sentence>"}."""
    return JSONResponse(
        status_code=exc.status_code, content={"error": exc.detail}, headers=exc.headers
    )


@app.exception_handler(RequestValidationError)
async def plain_validation_body(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Pydantic validation failures use the same envelope — never FastAPI's detail list."""
    first = exc.errors()[0] if exc.errors() else {}
    parts = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path")]
    field = parts[-1].replace("_", " ") if parts else "your input"
    if first.get("type") == "missing":
        message = f"Please fill in the {field}."
    else:
        message = f"Please check the {field}."
    return JSONResponse(status_code=422, content={"error": message})


@app.exception_handler(Exception)
async def plain_server_error(request: Request, exc: Exception) -> JSONResponse:
    reference = report_error(exc, where=f"api.{request.url.path}")
    return JSONResponse(
        status_code=500,
        content={"error": f"Something went wrong on our end. Reference: {reference}"},
    )


# Everything under /api so API routes can never collide with app screen URLs.
app.include_router(site_check_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(client_errors_router, prefix="/api")


@app.get("/health")
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


# Any /api/* path not matched by a router above is unknown — never fall
# back to serving a screen for it.
@app.api_route("/api/{_unmatched:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def unknown_api_route(_unmatched: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "We couldn't find that."})


# Serve the built screens. Any other path that isn't a real file in
# web/dist falls back to index.html, so client-side routes (e.g.
# /public-data/history) work on a hard refresh.
_WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"
if _WEB_DIST.is_dir():

    @app.get("/{full_path:path}")
    def serve_app(full_path: str) -> Response:
        candidate = (_WEB_DIST / full_path).resolve()
        if (
            full_path
            and candidate.is_file()
            and _WEB_DIST in candidate.parents
        ):
            return FileResponse(candidate)
        return FileResponse(_WEB_DIST / "index.html")
