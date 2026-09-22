"""
Keep Supabase Auth redirect allow list in sync with EGDesk custom domains.

Visitor OAuth sends redirectTo to the site that started login. If that URL is not in
uri_allow_list, GoTrue ignores it and sends the browser to site_url (https://egdesk.cloud).

site_url stays the fallback. This module only adds or removes allow-list entries.
The Management API token stays on the tunnel gateway (SUPABASE_ACCESS_TOKEN).
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

# Hosts whose redirect URLs are configured once in the Supabase dashboard.
_PLATFORM_HOSTS = {
    "localhost",
    "127.0.0.1",
    "[::1]",
    "tunneling-service.onrender.com",
    "egdesk.cloud",
    "www.egdesk.cloud",
}

_synced_hosts: set[str] = set()
_lock = asyncio.Lock()
_missing_token_logged = False


def parse_allow_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        parts = [str(item).strip() for item in value]
    else:
        parts = [part.strip() for part in str(value).split(",")]
    return [part for part in parts if part]


def format_allow_list(entries: list[str]) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        item = entry.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ",".join(ordered)


def redirect_pattern_for_host(host: str) -> str:
    return f"https://{host.strip().lower()}/**"


def merge_allow_list(current: object, patterns: list[str]) -> tuple[str, bool]:
    existing = parse_allow_list(current)
    merged = format_allow_list([*existing, *patterns])
    return merged, merged != format_allow_list(existing)


def remove_allow_patterns(current: object, patterns: list[str]) -> tuple[str, bool]:
    drop = {pattern.strip() for pattern in patterns if pattern.strip()}
    kept = [entry for entry in parse_allow_list(current) if entry not in drop]
    formatted = format_allow_list(kept)
    return formatted, formatted != format_allow_list(parse_allow_list(current))


def host_needing_allowlist(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return None
    if not host or host in _PLATFORM_HOSTS or host.endswith(".egdesk.cloud"):
        return None
    return host


def project_ref_from_env() -> str:
    explicit = (os.getenv("SUPABASE_PROJECT_REF") or "").strip()
    if explicit:
        return explicit
    host = urlparse(os.getenv("SUPABASE_URL") or "").hostname or ""
    if host.endswith(".supabase.co"):
        return host.split(".")[0]
    raise RuntimeError("Set SUPABASE_PROJECT_REF or SUPABASE_URL to update Auth redirect URLs.")


def _access_token() -> str | None:
    token = (os.getenv("SUPABASE_ACCESS_TOKEN") or "").strip()
    return token or None


async def _get_allow_list(client: httpx.AsyncClient, ref: str, token: str) -> object:
    response = await client.get(
        f"https://api.supabase.com/v1/projects/{ref}/config/auth",
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Supabase Auth config GET failed ({response.status_code}): {response.text[:300]}"
        )
    data = response.json()
    return data.get("uri_allow_list")


async def _patch_allow_list(client: httpx.AsyncClient, ref: str, token: str, allow_list: str) -> None:
    response = await client.patch(
        f"https://api.supabase.com/v1/projects/{ref}/config/auth",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"uri_allow_list": allow_list},
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Supabase Auth config PATCH failed ({response.status_code}): {response.text[:300]}"
        )


async def update_auth_redirect_allowlist(hosts: list[str], *, remove: bool = False) -> dict:
    """Add or remove https://{host}/** for each host. Does not change site_url."""
    global _missing_token_logged
    normalized = []
    for host in hosts:
        cleaned = host_needing_allowlist(f"https://{host}") or ""
        # host_needing_allowlist rejects platform hosts; bare host via https:// works.
        if not cleaned:
            raw = host.strip().lower()
            if raw and raw not in _PLATFORM_HOSTS and not raw.endswith(".egdesk.cloud"):
                cleaned = raw
        if cleaned:
            normalized.append(cleaned)
    patterns = [redirect_pattern_for_host(host) for host in dict.fromkeys(normalized)]
    if not patterns:
        return {"updated": False, "patterns": []}

    token = _access_token()
    if not token:
        if not _missing_token_logged:
            print(
                "[visitor-auth] SUPABASE_ACCESS_TOKEN is not set. "
                "Custom-domain redirectTo will fall back to site_url (egdesk.cloud) "
                "until the Management API token is configured on the tunnel gateway."
            )
            _missing_token_logged = True
        return {"updated": False, "patterns": patterns, "error": "SUPABASE_ACCESS_TOKEN is not set"}

    ref = project_ref_from_env()
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as client:
        current = await _get_allow_list(client, ref, token)
        if remove:
            updated, changed = remove_allow_patterns(current, patterns)
        else:
            updated, changed = merge_allow_list(current, patterns)
        if changed:
            await _patch_allow_list(client, ref, token, updated)
            print(f"[visitor-auth] Supabase uri_allow_list {'removed' if remove else 'added'}: {', '.join(patterns)}")
    if remove:
        _synced_hosts.difference_update(normalized)
    else:
        _synced_hosts.update(normalized)
    return {"updated": changed, "patterns": patterns}


async def ensure_visitor_redirect_allowed(*urls: str) -> dict:
    """Allowlist custom-domain hosts used as OAuth redirectTo or returnTo. Cached per process."""
    hosts: list[str] = []
    for url in urls:
        host = host_needing_allowlist(url)
        if host and host not in _synced_hosts:
            hosts.append(host)
    if not hosts:
        return {"updated": False, "patterns": []}
    async with _lock:
        pending = [host for host in hosts if host not in _synced_hosts]
        if not pending:
            return {"updated": False, "patterns": []}
        return await update_auth_redirect_allowlist(pending)
