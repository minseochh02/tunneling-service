"""
FedEx Basic Integrated Visibility (Track catalog).

Env: see fedex_config.py (FEDEX_CLIENT_ID, FEDEX_CLIENT_SECRET, …)
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from fedex_auth import get_access_token
from fedex_config import credentials_from_env

fedex_router = APIRouter(prefix="/fedex", tags=["FedEx"])


def _normalize_tracking_numbers(raw: str) -> list[str]:
    parts: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        value = "".join(chunk.split())
        if value:
            parts.append(value)
    return parts[:30]


def _summarize_track_result(data: dict[str, Any]) -> list[dict[str, Any]]:
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
    trackingNumber: str | None = Query(None, description="Single FedEx tracking number"),
    trackingNumbers: str | None = Query(None, description="Comma-separated (max 30)"),
    detailed: bool = Query(True, description="includeDetailedScans"),
    raw: bool = Query(False, description="Include full FedEx JSON"),
    tenant_id: str = Query("default", description="Tenant id for future per-tenant track creds"),
):
    """Track by FedEx tracking number(s) — Basic Integrated Visibility."""
    raw_list = trackingNumbers or trackingNumber or ""
    numbers = _normalize_tracking_numbers(raw_list)
    if not numbers:
        return JSONResponse(
            status_code=400,
            content={"error": "trackingNumber or trackingNumbers is required."},
        )

    creds = credentials_from_env(tenant_id)
    if not creds:
        return JSONResponse(
            status_code=503,
            content={"error": "Set FEDEX_CLIENT_ID and FEDEX_CLIENT_SECRET on tunneling-service."},
        )

    token, err = await get_access_token(creds)
    if err:
        return JSONResponse(status_code=503, content={"error": err})

    body = {
        "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": num}} for num in numbers],
        "includeDetailedScans": detailed,
    }
    base = creds.base_url()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-locale": creds.locale,
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
