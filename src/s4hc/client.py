"""Read-only OData V2 client for SAP S/4HANA Cloud Public Edition.

GET only. Handles Basic / X.509 auth, JSON format, $top/$skip paging,
__next links, retry with backoff on 429/5xx, SAP /Date()/ normalisation,
a hard row ceiling and one wall-clock deadline per paged read. Never follows
redirects (they could carry the credentials elsewhere). Never logs
credentials or row data.
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable

import requests

from . import config
from .errors import S4HCError

__all__ = ["S4HCClient", "S4HCError", "normalize_row", "normalize_value", "odata_str", "validate_fields", "validate_id"]

log = logging.getLogger("s4hc-mcp.client")

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-./ ]{1,40}$")
_SAP_DATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d+)?\)/$")
RETRY_STATUS = {429, 502, 503, 504}
MAX_RETRY_WAIT = 30.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def odata_str(value: str) -> str:
    """Quote a string for an OData $filter literal (escapes single quotes)."""
    return "'" + str(value).replace("'", "''") + "'"


def validate_id(value: str, what: str) -> str:
    v = str(value).strip()
    if not _SAFE_ID.match(v):
        raise S4HCError(f"Invalid {what}: {value!r}", "INVALID_SCOPE")
    return v


def validate_fields(fields: Iterable[str], allowed: Iterable[str], what: str) -> list[str]:
    allowed_set = set(allowed)
    if not config.person_fields_allowed():
        allowed_set -= config.PERSON_FIELDS
    out: list[str] = []
    for f in fields:
        f = str(f).strip()
        if f in config.PERSON_FIELDS and not config.person_fields_allowed():
            raise S4HCError(f"{f} identifies a person and is not exposed on this deployment "
                            "(S4HC_EXPOSE_PERSON_FIELDS=false)", "UNSUPPORTED_FIELD")
        if f not in allowed_set:
            raise S4HCError(f"Unknown {what} field {f!r}. Allowed: {sorted(allowed_set)}", "UNSUPPORTED_FIELD")
        if f not in out:
            out.append(f)
    return out


def normalize_value(v: Any) -> Any:
    """Turn SAP OData V2 '/Date(ms)/' strings into ISO dates; leave the rest."""
    if isinstance(v, str):
        m = _SAP_DATE.match(v)
        if m:
            ms = int(m.group(1))
            try:
                dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return v
            return dt.strftime("%Y-%m-%d") if dt.hour == 0 and dt.minute == 0 and dt.second == 0 else dt.isoformat()
    return v


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    row.pop("__metadata", None)
    for k, v in list(row.items()):
        if isinstance(v, dict):
            if "results" in v and isinstance(v["results"], list):
                v["results"] = [normalize_row(r) for r in v["results"] if isinstance(r, dict)]
            elif "__deferred" in v:
                row.pop(k)
            else:
                row[k] = normalize_row(v)
        else:
            row[k] = normalize_value(v)
    return row


def parse_entity_types(xml: str) -> dict[str, str]:
    """EntityType name -> its XML body, in one linear pass (a lazy regex over an
    unclosed tag is quadratic)."""
    out: dict[str, str] = {}
    pos = 0
    while True:
        start = xml.find("<EntityType ", pos)
        if start < 0:
            return out
        end = xml.find("</EntityType>", start)
        if end < 0:
            return out
        head_end = xml.find(">", start, end)
        m = re.match(r'<EntityType Name="([^"]+)"', xml[start:head_end + 1])
        if m:
            out[m.group(1)] = xml[head_end + 1:end]
        pos = end + len("</EntityType>")


def _retry_after(value: str | None, attempt: int) -> float:
    """Seconds to wait before a retry: Retry-After as seconds or an HTTP-date,
    else exponential backoff. Never negative, never above MAX_RETRY_WAIT."""
    fallback = 0.5 * 2**attempt
    if value:
        try:
            wait = float(value)
        except ValueError:
            try:
                wait = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, IndexError, OverflowError):
                wait = fallback
        if not math.isfinite(wait):
            wait = fallback
    else:
        wait = fallback
    return min(max(wait, 0.0), MAX_RETRY_WAIT)


def _close(r: Any) -> None:
    close = getattr(r, "close", None)
    if callable(close):
        close()


def _read_capped(r: Any) -> Any:
    """Read a streamed body, refusing it past MAX_RESPONSE_BYTES whatever the
    server declares (chunked bodies have no Content-Length)."""
    try:
        declared = int(r.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > MAX_RESPONSE_BYTES:
        _close(r)
        raise S4HCError(f"Response of {declared} bytes exceeds the {MAX_RESPONSE_BYTES} byte limit", "QUERY_TOO_LARGE")
    if not isinstance(r, requests.Response):
        return r
    chunks, total = [], 0
    for chunk in r.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            _close(r)
            raise S4HCError(f"Response exceeds the {MAX_RESPONSE_BYTES} byte limit", "QUERY_TOO_LARGE")
        chunks.append(chunk)
    r._content = b"".join(chunks)
    r._content_consumed = True
    return r


class S4HCClient:
    def __init__(
        self,
        base_url: str,
        user: str | None = None,
        password: str | None = None,
        cert: str | tuple[str, str] | None = None,
        timeout: int = 60,
        retries: int = 3,
        on_verified: Callable[[], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._on_verified = on_verified
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        if cert:
            self.session.cert = cert
        elif user and password:
            self.session.auth = (user, password)
        else:
            raise S4HCError(
                "No credentials: set S4HC_USER + S4HC_PASSWORD or S4HC_CLIENT_CERT(_PEM)", "NOT_CONFIGURED"
            )
        self.session.headers.update({"Accept": "application/json"})
        self.verified = False  # set once SAP has accepted these credentials

    # ------------------------------------------------------------------ http
    def _request(self, url: str, params: dict[str, Any] | None = None, accept: str = "application/json",
                 deadline: float | None = None):
        deadline = deadline or time.monotonic() + config.get_request_deadline()
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                r = self.session.get(url, params=params, headers={"Accept": accept},
                                     timeout=min(self.timeout, remaining), allow_redirects=False, stream=True)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(min(0.5 * 2**attempt, max(deadline - time.monotonic(), 0)))
                    continue
                raise S4HCError(f"Connection error to S/4HANA Cloud: {exc.__class__.__name__}", "SAP_UNAVAILABLE") from exc
            if r.status_code in RETRY_STATUS and attempt < self.retries:
                wait = _retry_after(r.headers.get("Retry-After"), attempt)
                if time.monotonic() + wait >= deadline:
                    return _read_capped(r)
                _close(r)
                log.warning("HTTP %s from S/4HANA Cloud, retrying in %.1fs", r.status_code, wait)
                time.sleep(wait)
                continue
            return _read_capped(r)
        if last_exc is not None:
            raise S4HCError(f"Request failed after retries: {last_exc.__class__.__name__}", "SAP_UNAVAILABLE")
        raise S4HCError(f"S/4HANA Cloud did not answer within {config.get_request_deadline():.0f}s", "SAP_UNAVAILABLE")

    def _raise_for_status(self, r: requests.Response, url: str) -> None:
        if r.status_code < 300:
            if not self.verified:
                self.verified = True
                if self._on_verified:
                    self._on_verified()
            return
        path = url.replace(self.base_url, "").split("?")[0]
        if r.status_code < 400:
            raise S4HCError(
                f"HTTP {r.status_code} redirect on {path}, not followed — S4HC_BASE_URL must be the "
                "tenant's API host (https://<tenant>-api.s4hana.cloud.sap), not the Fiori launchpad host",
                "NOT_CONFIGURED")
        if r.status_code == 401:
            raise S4HCError("401 Unauthorized — check the communication user / certificate", "SAP_UNAUTHORIZED")
        if r.status_code == 403:
            raise S4HCError(
                f"403 Forbidden on {path} — no active communication arrangement exposes this "
                "service to this communication user (see docs/CONNECTION_INPUTS.md for scenario IDs)",
                "SAP_FORBIDDEN",
            )
        if r.status_code == 404:
            raise S4HCError(f"404 — service or entity not found: {path}", "SAP_NOT_FOUND")
        if r.status_code == 429:
            raise S4HCError("SAP rate limit reached; retry later", "SAP_RATE_LIMITED")
        if r.status_code >= 500:
            raise S4HCError(f"SAP returned HTTP {r.status_code}", "SAP_UNAVAILABLE")
        msg = ""
        try:
            msg = r.json().get("error", {}).get("message", {}).get("value", "")
        except Exception:  # noqa: BLE001
            msg = (r.text or "")[:300]
        raise S4HCError(f"HTTP {r.status_code}: {msg}", "INVALID_SCOPE")

    def _get(self, url: str, params: dict[str, Any] | None = None, deadline: float | None = None) -> dict[str, Any]:
        r = self._request(url, params, deadline=deadline)
        self._raise_for_status(r, url)
        try:
            data = r.json()
        except ValueError as exc:
            raise S4HCError("Non-JSON response from S/4HANA Cloud", "SAP_UNAVAILABLE") from exc
        return data if isinstance(data, dict) else {}

    def get_metadata(self, service_path: str) -> str:
        url = f"{self.base_url}{service_path}/$metadata"
        r = self._request(url, accept="application/xml")
        self._raise_for_status(r, url)
        return r.text

    def probe(self, service_path: str, entity_set: str) -> None:
        """Read one row (or none) to prove the user may *start* the service:
        $metadata can be readable while data access is still forbidden."""
        url = f"{self.base_url}{service_path}/{entity_set}"
        self._get(url, {"$format": "json", "$top": 1})

    # ---------------------------------------------------------------- paging
    def get_all(
        self,
        service_path: str,
        entity_set: str,
        *,
        select: list[str] | None = None,
        filter_: str | None = None,
        expand: list[str] | None = None,
        orderby: str | None = None,
        top: int | None = None,
        max_rows: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return (rows, truncated). Follows SAP's __next links whenever they
        are present; otherwise pages with $top/$skip. One extra row beyond the
        ceiling is requested so `truncated` is exact, not a guess."""
        page = config.get_page_size()
        ceiling = min(max_rows or config.get_max_rows(), top or 10**9)
        want = ceiling + 1
        deadline = time.monotonic() + config.get_request_deadline()
        url = f"{self.base_url}{service_path}/{entity_set}"
        params: dict[str, Any] = {"$format": "json", "$top": min(page, want), "$skip": 0}
        if select:
            params["$select"] = ",".join(select)
        if filter_:
            params["$filter"] = filter_
        if expand:
            params["$expand"] = ",".join(expand)
        if orderby:
            params["$orderby"] = orderby

        rows: list[dict[str, Any]] = []
        next_url: str | None = None
        followed_next = False
        seen_links: set[str] = set()
        pages = 0
        allowed_prefix = f"{self.base_url}{service_path}/"
        while True:
            pages += 1
            if pages > config.get_max_pages():
                raise S4HCError(
                    f"More than {config.get_max_pages()} pages needed; narrow the filter", "QUERY_TOO_LARGE")
            asked = params["$top"]
            data = self._get(next_url or url, None if next_url else params, deadline=deadline)
            body = data.get("d", {})
            body = body if isinstance(body, dict) else {}
            results = body.get("results", [])
            if not isinstance(results, list):
                results = []
            for row in results:
                if isinstance(row, dict):
                    rows.append(normalize_row(row))
            if len(rows) >= want:
                return rows[:ceiling], True
            next_link = body.get("__next")
            if next_link:
                # Follow continuation links only on the configured origin AND service path,
                # never with embedded credentials, never the same link twice.
                if not isinstance(next_link, str) or not next_link.startswith(allowed_prefix) \
                        or "@" in next_link.split("?")[0]:
                    raise S4HCError("SAP returned a continuation link outside the configured service", "INCOMPLETE_RESULT")
                if next_link in seen_links:
                    raise S4HCError("SAP returned a repeated continuation link", "INCOMPLETE_RESULT")
                seen_links.add(next_link)
                next_url, followed_next = next_link, True
                continue
            if followed_next or len(results) < asked:
                return rows, False
            params["$skip"] += asked
            params["$top"] = min(page, want - len(rows))
