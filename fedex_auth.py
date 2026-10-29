"""FedEx OAuth 2.0 client credentials (per-tenant token cache)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from fedex_config import FedExCredentials

_token_cache: dict[str, dict[str, Any]] = {}


async def get_access_token(creds: FedExCredentials) -> tuple[str | None, str | None]:
    key = creds.cache_key()
    now = time.time()
    cached = _token_cache.get(key) or {}
    token = (cached.get("access_token") or "").strip()
    if token and now < float(cached.get("expires_at") or 0) - 30:
        return token, None

    base = creds.base_url()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/oauth/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
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
    _token_cache[key] = {"access_token": token, "expires_at": now + expires_in}
    return token, None
