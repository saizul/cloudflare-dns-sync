"""Minimal Cloudflare API v4 client for DNS records (httpx, API token auth)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from cloudflare_dns_sync._version import __version__

DEFAULT_API_URL = "https://api.cloudflare.com/client/v4"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class CloudflareError(Exception):
    """The Cloudflare API returned an error."""

    def __init__(self, message: str, status: int | None = None, errors: list[dict[str, Any]] | None = None):
        self.status = status
        self.errors = errors or []
        super().__init__(message)


def _error_message(response: httpx.Response) -> tuple[str, list[dict[str, Any]]]:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}", []
    errors = body.get("errors") or [] if isinstance(body, dict) else []
    parts = [f"{e.get('code', '?')}: {e.get('message', '')}".strip() for e in errors if isinstance(e, dict)]
    return (f"HTTP {response.status_code}: " + "; ".join(parts)) if parts else f"HTTP {response.status_code}", errors


class CloudflareClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_API_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": f"cloudflare-dns-sync/{__version__}",
                "Accept": "application/json",
            },
        )
        self._max_retries = max_retries
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CloudflareClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise CloudflareError(f"{method} {path}: {exc.__class__.__name__}: {exc}") from exc
                attempt += 1
                self._sleep(min(2**attempt, 30))
                continue
            if response.status_code in RETRY_STATUS and attempt < self._max_retries:
                attempt += 1
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else float(min(2**attempt, 30))
                self._sleep(delay)
                continue
            if response.status_code >= 400:
                message, errors = _error_message(response)
                raise CloudflareError(f"{method} {path}: {message}", response.status_code, errors)
            try:
                body = response.json()
            except ValueError as exc:
                raise CloudflareError(f"{method} {path}: response is not JSON", response.status_code) from exc
            if not isinstance(body, dict) or body.get("success") is False:
                message, errors = _error_message(response)
                raise CloudflareError(f"{method} {path}: {message}", response.status_code, errors)
            return body

    # -- zones -----------------------------------------------------------

    def zone_id(self, name: str) -> str:
        body = self._request("GET", "/zones", params={"name": name, "per_page": 50})
        for zone in body.get("result") or []:
            if str(zone.get("name", "")).lower() == name.lower():
                return str(zone["id"])
        raise CloudflareError(
            f"zone {name} not found (check the name and that the token has Zone:Read access to it)", 404
        )

    # -- records ---------------------------------------------------------

    def list_records(self, zone_id: str, per_page: int = 100) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            body = self._request("GET", f"/zones/{zone_id}/dns_records", params={"page": page, "per_page": per_page})
            records.extend(body.get("result") or [])
            info = body.get("result_info") or {}
            total_pages = int(info.get("total_pages") or 1)
            if page >= total_pages or not body.get("result"):
                return records
            page += 1

    def create_record(self, zone_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/zones/{zone_id}/dns_records", json=payload)["result"]

    def update_record(self, zone_id: str, record_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=payload)["result"]

    def delete_record(self, zone_id: str, record_id: str) -> None:
        self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
