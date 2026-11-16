"""Offline replay harness for the §13.2 per-source adapter contract tests.

Shared between the one-shot fixture recorder
(`tests/record_site_check_fixtures.py`, run manually against the live
providers) and the offline pytest suite
(`tests/test_site_check_adapter_contracts.py`).

Design (site_check.md §13.2): every registry source has a captured RAW
provider payload set under `tests/fixtures/site_check/<source_id>/fixture.json`.
The test drives the adapter's real `fetch()` through a `ReplaySession`
that serves those recorded HTTP exchanges and refuses to touch the
network, then asserts the full AdapterResult contract against the
expected dump captured at record time.

Determinism levers:
- request matching is keyed on (method, url, params, body) with secret
  query values (ArcGIS `token`, Census `key`, ...) redacted on both
  sides, so fixtures carry no credentials and replay matches regardless
  of the runtime key value;
- adapter-module `datetime`/`date` classes are frozen to the recorded
  instant (`freeze_adapter_clocks`), so age math and `fetched_at` are
  reproducible years later;
- backoff settings are zeroed by the test so recorded failure exchanges
  replay without real sleeps.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import importlib
import json
import pkgutil
from collections import defaultdict, deque
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests
from requests.structures import CaseInsensitiveDict

from app.services.site_check.usage import note_session_request

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "site_check"

# Query/body parameter names whose values are secrets or otherwise
# non-reproducible. Redacted symmetrically at record and replay time.
SECRET_PARAM_NAMES = frozenset({
    "token", "key", "apikey", "api_key", "x-api-key", "subscription-key",
})

_REDACTED = "REDACTED"


# ── Request canonicalization ─────────────────────────────────────────


def _redact_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        (k, _REDACTED if k.lower() in SECRET_PARAM_NAMES else v)
        for k, v in pairs
    ]


def redact_url(url: str) -> str:
    """URL with any secret query values replaced — safe to store."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = _redact_pairs(parse_qsl(parts.query, keep_blank_values=True))
    return parts._replace(query=urlencode(pairs)).geturl()


def _body_digest(data, json_body) -> str:
    if data is None and json_body is None:
        return "-"
    if isinstance(data, dict):
        pairs = sorted(
            _redact_pairs([(str(k), str(v)) for k, v in data.items()])
        )
        return "d:" + urlencode(pairs)
    if isinstance(data, (str, bytes)):
        raw = data.encode("utf-8") if isinstance(data, str) else data
        return "r:" + hashlib.sha256(raw).hexdigest()
    if json_body is not None:
        blob = json.dumps(json_body, sort_keys=True, default=str)
        return "j:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return "o:" + hashlib.sha256(repr(data).encode("utf-8")).hexdigest()


def normalize_key(
    method: str,
    url: str,
    params=None,
    data=None,
    json_body=None,
) -> str:
    """Canonical match key for one logical HTTP request.

    Merges the URL query string with the `params` kwarg, sorts pairs,
    redacts secret values, and hashes any request body. The same
    function runs at record and replay time, so a fixture recorded with
    a real ArcGIS token matches a replay running with a dummy one.
    """
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if isinstance(params, dict):
        pairs += [(str(k), str(v)) for k, v in params.items()]
    elif params:
        pairs += [(str(k), str(v)) for k, v in params]
    canonical_query = urlencode(sorted(_redact_pairs(pairs)))
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return (
        f"{method.upper()} {base}?{canonical_query}"
        f"#{_body_digest(data, json_body)}"
    )


def loose_key(method: str, url: str) -> str:
    parts = urlsplit(url)
    return f"{method.upper()} {parts.scheme}://{parts.netloc}{parts.path}"


# ── Exchange capture / response reconstruction ───────────────────────


def exchange_from_response(
    method: str,
    url: str,
    kwargs: dict,
    resp: requests.Response,
    *,
    drop_body: bool = False,
) -> dict:
    """Serialize one live response into a storable fixture exchange."""
    ex: dict = {
        "method": method.upper(),
        "url": redact_url(resp.url or url),
        "key": normalize_key(
            method, url, kwargs.get("params"), kwargs.get("data"),
            kwargs.get("json"),
        ),
        "loose_key": loose_key(method, url),
        "status": resp.status_code,
        "headers": {
            k: v for k, v in resp.headers.items()
            if k.lower() in ("content-type", "retry-after")
        },
    }
    if drop_body:
        ex["body_dropped"] = True
        return ex
    content = resp.content or b""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and "\x00" not in text:
        ex["body_text"] = text
    else:
        ex["body_b64"] = base64.b64encode(content).decode("ascii")
    return ex


def exchange_from_exception(
    method: str, url: str, kwargs: dict, exc: Exception,
) -> dict:
    return {
        "method": method.upper(),
        "url": redact_url(url),
        "key": normalize_key(
            method, url, kwargs.get("params"), kwargs.get("data"),
            kwargs.get("json"),
        ),
        "loose_key": loose_key(method, url),
        "exception": type(exc).__name__,
        "exception_message": "recorded upstream failure (replayed)",
    }


def build_response(exchange: dict, url: str) -> requests.Response:
    resp = requests.Response()
    resp.status_code = int(exchange["status"])
    resp.headers = CaseInsensitiveDict(exchange.get("headers") or {})
    if exchange.get("body_dropped"):
        content = b""
    elif "body_b64" in exchange:
        content = base64.b64decode(exchange["body_b64"])
    else:
        content = (exchange.get("body_text") or "").encode("utf-8")
    resp._content = content
    resp._content_consumed = True  # iter_content() serves cached chunks
    resp.url = exchange.get("url") or url
    resp.reason = ""
    return resp


class ReplayMiss(AssertionError):
    """No recorded exchange matches the replayed request."""


class ReplaySession(requests.Session):
    """Serves recorded exchanges; never opens a socket.

    Matching: exact canonical key first (FIFO across duplicates, the
    last exchange for a key is repeatable), then a loose
    method+scheme+host+path fallback in recorded order — a safety net
    for benign param drift. A miss raises ReplayMiss so the test fails
    loudly instead of silently hitting the live provider.

    Provider metering: the session carries the same
    `provider_call_collector` hook as the hardened runtime session, so
    §8.6 request accounting can be asserted offline against the exact
    request sequence an adapter really makes — including the attempts
    that failed and the ones that returned nothing.
    """

    def __init__(
        self, exchanges: list[dict], *, collector=None,
    ):
        super().__init__()
        self.provider_call_collector = collector
        self._by_key: dict[str, deque] = defaultdict(deque)
        self._by_loose: dict[str, deque] = defaultdict(deque)
        self._last_by_key: dict[str, dict] = {}
        for ex in exchanges:
            self._by_key[ex["key"]].append(ex)
            self._by_loose[ex["loose_key"]].append(ex)
            self._last_by_key[ex["key"]] = ex
        # `_resilient_http` sends through this sibling when present.
        self.no_transport_retry = self

    def request(self, method, url, **kwargs):  # type: ignore[override]
        note_session_request(self, url)
        key = normalize_key(
            method, url, kwargs.get("params"), kwargs.get("data"),
            kwargs.get("json"),
        )
        ex = None
        bucket = self._by_key.get(key)
        if bucket:
            ex = bucket.popleft()
        elif key in self._last_by_key:
            ex = self._last_by_key[key]
        else:
            loose = self._by_loose.get(loose_key(method, url))
            while loose:
                candidate = loose.popleft()
                if self._by_key.get(candidate["key"]) is not None and (
                    candidate in self._by_key[candidate["key"]]
                ):
                    self._by_key[candidate["key"]].remove(candidate)
                    ex = candidate
                    break
        if ex is None:
            raise ReplayMiss(
                f"no recorded exchange for {method.upper()} {redact_url(url)}"
            )
        if "exception" in ex:
            exc_cls = getattr(
                requests.exceptions, ex["exception"],
                requests.exceptions.ConnectionError,
            )
            raise exc_cls(ex.get("exception_message") or ex["exception"])
        return build_response(ex, url)


# ── Adapter-module clock freezing ────────────────────────────────────


def _adapter_modules():
    import app.services.site_check.adapters as pkg

    modules = []
    for info in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        modules.append(importlib.import_module(info.name))
    return modules


def freeze_adapter_clocks(instant: datetime):
    """Pin `datetime.now()` / `date.today()` inside every adapter module
    to the recorded instant. Returns an undo callable."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return instant.astimezone(tz)
            return instant.astimezone(UTC).replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return instant.astimezone(UTC).replace(tzinfo=None)

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return instant.astimezone(UTC).date()

    undo: list[tuple[object, str, object]] = []
    for mod in _adapter_modules():
        if getattr(mod, "datetime", None) is datetime:
            undo.append((mod, "datetime", datetime))
            mod.datetime = _FrozenDatetime
        if getattr(mod, "date", None) is date:
            undo.append((mod, "date", date))
            mod.date = _FrozenDate

    def restore():
        for mod, name, original in undo:
            setattr(mod, name, original)

    return restore


# ── Fixture IO + replay ──────────────────────────────────────────────


def fixture_path(source_id: str) -> Path:
    return FIXTURE_ROOT / source_id / "fixture.json"


def write_gzip_json(path: Path, obj) -> None:
    """Deterministic gzip (mtime=0) of a JSON document."""
    raw = json.dumps(obj, indent=1).encode("utf-8")
    with path.open("wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
        gz.write(raw)


def _read_gzip_json(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def load_fixture(source_id: str) -> dict:
    with fixture_path(source_id).open() as fh:
        fixture = json.load(fh)
    if "exchanges" not in fixture:
        # Raw provider exchanges live in a gzip sidecar (they dominate
        # fixture bytes; the meta + expected dump stay readable JSON).
        fixture["exchanges"] = _read_gzip_json(
            FIXTURE_ROOT / source_id / fixture["exchanges_file"]
        )
    return fixture


def load_stage_results(stage: str) -> dict:
    """Shared per-wave upstream-results snapshot recorded alongside the
    fixtures (config inputs for wave-2/3 adapters)."""
    if stage == "initial":
        return {}
    return _read_gzip_json(
        FIXTURE_ROOT / "_shared" / f"results_{stage}.json.gz"
    )


def replay_fetch(source_id: str, fixture: dict, *, collector=None):
    """Drive the real adapter `fetch` over the recorded exchanges with
    the recorded identity + orchestrator-built config. Caller is
    responsible for clock freezing / settings overrides. Pass a
    `ProviderCallCollector` to also capture the §8.6 request ledger the
    replayed adapter produces."""
    from app.services.site_check.identity import PropertyIdentity
    from app.services.site_check.orchestrator import (
        _build_adapter_config,
        _load_adapter_fetch,
    )

    identity = PropertyIdentity(**fixture["identity"])
    config = _build_adapter_config(
        source_id,
        load_stage_results(fixture["stage"]),
        project_context={},
    )
    session = ReplaySession(fixture["exchanges"], collector=collector)
    fetch = _load_adapter_fetch(source_id)
    return fetch(identity, session=session, config=config)


# ── Expected-vs-actual comparison ────────────────────────────────────

# Always pruned before comparison: wall-clock stamped by the adapter.
ALWAYS_IGNORED_PATHS = ("fetched_at",)


def prune_paths(obj: dict, paths) -> dict:
    """Deep-copy `obj` with each dotted path removed (missing paths are
    fine). List segments use `*` to apply to every element."""
    out = json.loads(json.dumps(obj))

    def _remove(node, segments):
        if not segments:
            return
        head, rest = segments[0], segments[1:]
        if isinstance(node, list):
            if head == "*":
                for item in node:
                    _remove(item, rest)
            return
        if not isinstance(node, dict):
            return
        if head == "*":
            for value in node.values():
                _remove(value, rest)
            return
        if head not in node:
            return
        if not rest:
            node.pop(head, None)
        else:
            _remove(node[head], rest)

    for path in paths:
        _remove(out, path.split("."))
    return out


def comparable_dump(result_dump: dict, fixture: dict) -> dict:
    ignore = list(ALWAYS_IGNORED_PATHS) + list(
        fixture.get("ignore_paths") or []
    )
    return prune_paths(result_dump, ignore)
