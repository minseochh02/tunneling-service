"""
FedEx Basic Integrated Visibility (Track catalog).

Env:
  FEDEX_CLIENT_ID      Project API Key from FedEx Developer Portal
  FEDEX_CLIENT_SECRET  Project Secret Key
  FEDEX_API_BASE_URL   optional; default https://apis-sandbox.fedex.com
                       production: https://apis.fedex.com
  FEDEX_USE_PRODUCTION if "true"/"1", use https://apis.fedex.com (overrides default sandbox)
  FEDEX_LOCALE         optional x-locale header (default en_US)
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

_DEFAULT_SANDBOX = "https://apis-sandbox.fedex.com"
_PRODUCTION = "https://apis.fedex.com"

fedex_router = APIRouter(prefix="/fedex", tags=["FedEx"])

_token_cache: dict[str, Any] = {"access_token": "", "expires_at": 0.0}


def _api_base() -> str:
    explicit = (os.getenv("FEDEX_API_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    use_prod = (os.getenv("FEDEX_USE_PRODUCTION") or "").lower() in ("1", "true", "yes")
    return _PRODUCTION if use_prod else _DEFAULT_SANDBOX


def _credentials() -> tuple[str, str]:
    client_id = (os.getenv("FEDEX_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("FEDEX_CLIENT_SECRET") or "").strip()
    return client_id, client_secret


async def _get_access_token() -> tuple[str | None, str | None]:
    """Return (token, error_message)."""
    client_id, client_secret = _credentials()
    if not client_id or not client_secret:
        return None, "Set FEDEX_CLIENT_ID and FEDEX_CLIENT_SECRET on tunneling-service."

    now = time.time()
    cached = _token_cache.get("access_token") or ""
    if cached and now < float(_token_cache.get("expires_at") or 0) - 30:
        return cached, None

    base = _api_base()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/oauth/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if resp.status_code >= 400:
        return None, f"OAuth failed HTTP {resp.status_code}: {(resp.text or '')[:300]}"

    try:
        payload = resp.json()
    except ValueError:
        return None, "OAuth response was not JSON."

    token = (payload.get("access_token") or "").strip()
    if not token:
        return None, "OAuth response missing access_token."

    expires_in = int(payload.get("expires_in") or 3600)
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = now + expires_in
    return token, None


def _normalize_tracking_numbers(raw: str) -> list[str]:
    parts: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        value = "".join(chunk.split())
        if value:
            parts.append(value)
    # FedEx allows up to 30 per request
    return parts[:30]


def _summarize_track_result(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten FedEx output into easy rows for sheets/automation."""
    out: list[dict[str, Any]] = []
    output = data.get("output") or {}
    complete = output.get("completeTrackResults") or []
    for block in complete:
        tn = (block.get("trackingNumber") or "").strip()
        results = block.get("trackResults") or []
        for tr in results:
            latest = tr.get("latestStatusDetail") or {}
            scan_events = tr.get("scanEvents") or []
            dates = tr.get("dateAndTimes") or []
            row: dict[str, Any] = {
                "trackingNumber": tn,
                "code": latest.get("code"),
                "status": latest.get("description") or latest.get("statusByLocale"),
                "derivedStatus": tr.get("derivedStatus"),
                "serviceType": tr.get("serviceDetail", {}).get("type")
                if isinstance(tr.get("serviceDetail"), dict)
                else tr.get("serviceType"),
            }
            if tr.get("error"):
                row["error"] = tr.get("error")
            if dates:
                row["dateAndTimes"] = dates
            if scan_events:
                row["scanEvents"] = [
                    {
                        "date": e.get("date"),
                        "eventType": e.get("eventType"),
                        "eventDescription": e.get("eventDescription"),
                        "derivedStatus": e.get("derivedStatus"),
                        "scanLocation": e.get("scanLocation"),
                    }
                    for e in scan_events[:50]
                ]
            out.append(row)
    return out


@fedex_router.get("/track")
async def track_shipments(
    trackingNumber: str | None = Query(
        None,
        description="Single FedEx tracking number",
    ),
    trackingNumbers: str | None = Query(
        None,
        description="Comma-separated tracking numbers (max 30)",
    ),
    detailed: bool = Query(True, description="includeDetailedScans in FedEx request"),
    raw: bool = Query(False, description="Include full FedEx JSON in response"),
):
    """Track by FedEx tracking number(s) — Basic Integrated Visibility."""
    raw_list = trackingNumbers or trackingNumber or ""
    numbers = _normalize_tracking_numbers(raw_list)
    if not numbers:
        return JSONResponse(
            status_code=400,
            content={"error": "trackingNumber or trackingNumbers is required."},
        )

    token, err = await _get_access_token()
    if err:
        return JSONResponse(status_code=503, content={"error": err})

    body = {
        "trackingInfo": [
            {"trackingNumberInfo": {"trackingNumber": num}} for num in numbers
        ],
        "includeDetailedScans": detailed,
    }
    base = _api_base()
    locale = (os.getenv("FEDEX_LOCALE") or "en_US").strip() or "en_US"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-locale": locale,
        "x-customer-transaction-id": str(uuid.uuid4()),
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{base}/track/v1/trackingnumbers",
                headers=headers,
                json=body,
            )
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"error": f"{type(exc).__name__}: {exc}"})

    text = resp.text or ""
    if resp.status_code >= 400:
        return JSONResponse(
            status_code=502,
            content={
                "error": "FedEx track request failed.",
                "httpStatus": resp.status_code,
                "apiBase": base,
                "body": text[:800],
            },
        )

    try:
        data = resp.json()
    except ValueError:
        return JSONResponse(
            status_code=502,
            content={"error": "FedEx response was not JSON.", "body": text[:300]},
        )

    summary = _summarize_track_result(data)
    print(f"📦 FedEx track n={len(numbers)} results={len(summary)} base={base}")

    result: dict[str, Any] = {
        "queryable": bool(summary),
        "apiBase": base,
        "trackingNumbers": numbers,
        "shipments": summary,
    }
    if raw:
        result["fedex"] = data
    return result
