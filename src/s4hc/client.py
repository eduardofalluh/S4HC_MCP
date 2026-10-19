"""Read-only OData V2 client for SAP S/4HANA Cloud Public Edition.

GET only. Handles Basic / X.509 auth, JSON format, $top/$skip paging,
__next links, retry with backoff on 429/5xx, SAP /Date()/ normalisation,
and a hard row ceiling. Never logs credentials or row data.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

from . import config

log = logging.getLogger("s4hc-mcp.client")

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-./ ]{1,40}$")
_SAP_DATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d+)?\)/$")
RETRY_STATUS = {429, 502, 503, 504}


class S4HCError(RuntimeError):
    """Application error with a stable code the agent can branch on.

    Codes: INVALID_SCOPE, UNAUTHORIZED_SCOPE, UNSUPPORTED_FIELD, PLAN_UNAVAILABLE,
    QUERY_TOO_LARGE, MIXED_CURRENCY, SAP_UNAUTHORIZED, SAP_FORBIDDEN, SAP_NOT_FOUND,
    SAP_RATE_LIMITED, SAP_UNAVAILABLE, INCOMPLETE_RESULT, METADATA_MISMATCH,
    NOT_CONFIGURED, DISABLED.
    """

    RETRYABLE = {"SAP_RATE_LIMITED", "SAP_UNAVAILABLE"}

    def __init__(self, message: str, code: str = "INVALID_SCOPE") -> None:
        self.code = code
        self.retryable = code in self.RETRYABLE
        super().__init__(f"[{code}] {message}")


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
    out: list[str] = []
    for f in fields:
        f = str(f).strip()
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


class S4HCClient:
    def __init__(
        self,
        base_url: str,
        user: str | None = None,
        password: str | None = None,
        cert: str | tuple[str, str] | None = None,
        timeout: int = 60,
        retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
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

    # ------------------------------------------------------------------ http
    def _request(self, url: str, params: dict[str, Any] | None = None, accept: str = "application/json"):
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.session.get(url, params=params, headers={"Accept": accept}, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(0.5 * 2**attempt)
                    continue
                raise S4HCError(f"Connection error to S/4HANA Cloud: {exc.__class__.__name__}", "SAP_UNAVAILABLE") from exc
            if r.status_code in RETRY_STATUS and attempt < self.retries:
                wait = float(r.headers.get("Retry-After") or 0.5 * 2**attempt)
                log.warning("HTTP %s from S/4HANA Cloud, retrying in %.1fs", r.status_code, wait)
                time.sleep(min(wait, 30))
                continue
            return r
        raise S4HCError(f"Request failed after retries: {last_exc.__class__.__name__ if last_exc else 'unknown'}", "SAP_UNAVAILABLE")

    def _raise_for_status(self, r: requests.Response, url: str) -> None:
        if r.status_code < 400:
            return
        path = url.replace(self.base_url, "").split("?")[0]
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

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self._request(url, params)
        self._raise_for_status(r, url)
        try:
            return r.json()
        except ValueError as exc:
            raise S4HCError("Non-JSON response from S/4HANA Cloud", "SAP_UNAVAILABLE") from exc

    def get_metadata(self, service_path: str) -> str:
        url = f"{self.base_url}{service_path}/$metadata"
        r = self._request(url, accept="application/xml")
        if r.status_code >= 400:
            self._raise_for_status(r, url)
        return r.text

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
        """Return (rows, truncated). Pages with $top/$skip or __next."""
        page = config.get_page_size()
        ceiling = min(max_rows or config.get_max_rows(), top or 10**9)
        url = f"{self.base_url}{service_path}/{entity_set}"
        params: dict[str, Any] = {"$format": "json", "$top": min(page, ceiling), "$skip": 0}
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
        seen_links: set[str] = set()
        pages = 0
        allowed_prefix = f"{self.base_url}{service_path}/"
        while True:
            pages += 1
            if pages > config.get_max_pages():
                raise S4HCError(
                    f"More than {config.get_max_pages()} pages needed; narrow the filter", "QUERY_TOO_LARGE")
            data = self._get(next_url or url, None if next_url else params)
            results = data.get("d", {}).get("results", [])
            if not isinstance(results, list):
                results = []
            for row in results:
                if isinstance(row, dict):
                    rows.append(normalize_row(row))
            if len(rows) >= ceiling:
                return rows[:ceiling], len(results) >= params["$top"]
            if len(results) < params["$top"]:
                return rows, False
            next_url = data.get("d", {}).get("__next")
            if next_url:
                # Follow continuation links only on the configured origin AND service path,
                # never with embedded credentials, never the same link twice.
                if not next_url.startswith(allowed_prefix) or "@" in next_url.split("?")[0]:
                    raise S4HCError("SAP returned a continuation link outside the configured service", "INCOMPLETE_RESULT")
                if next_url in seen_links:
                    raise S4HCError("SAP returned a repeated continuation link", "INCOMPLETE_RESULT")
                seen_links.add(next_url)
            if not next_url:
                params["$skip"] += params["$top"]
                params["$top"] = min(page, ceiling - len(rows))
