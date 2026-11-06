"""
Security helpers for the EGDesk tunneling-service gateway.

Covers:
1. Safe XML parsing (blocks DTD / XXE / entity expansion; uses defusedxml when installed).
2. Tunnel / server_key slug validation to prevent PostgREST `.or_()` filter injection.
3. Constant-time API key comparison and registered tunnel API key lookup.
4. Outbound webhook URL validation (SSRF protection for Sheet Sync callbacks).
5. Google Drive file/folder ID validation to prevent Drive API query injection.
6. Trusted client IP extraction behind Render / reverse proxies.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import socket
import xml.etree.ElementTree as StdET
from typing import Any
from urllib.parse import urlparse

try:
    from defusedxml import ElementTree as SafeET  # type: ignore[import-untyped]
except ImportError:
    SafeET = None

# Valid tunnel_id / server_key / name: alphanumeric, dot, underscore, hyphen (1..128 chars).
# Explicitly forbids commas, parentheses, quotes, colons, spaces, and slashes so PostgREST
# filter strings like `.or_(f"server_key.eq.{val},name.eq.{val}")` cannot be injected.
_TUNNEL_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Safe Google Drive / Spreadsheet / Script IDs
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Forbid XML DTD / entity declarations regardless of whether defusedxml is installed
_XML_ENTITY_DECL_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)

# Blocked cloud metadata hostnames
_BLOCKED_WEBHOOK_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "169.254.169.254",
        "100.100.100.200",
        "0.0.0.0",
    }
)


def safe_xml_fromstring(text: str) -> StdET.Element:
    """
    Parse XML string safely while rejecting DTD and entity declarations (XXE / Billion Laughs).
    Raises xml.etree.ElementTree.ParseError on malformed or unsafe XML.
    """
    raw = text or ""
    if _XML_ENTITY_DECL_RE.search(raw):
        raise StdET.ParseError("XML DTD and ENTITY declarations are not permitted")
    if SafeET is not None:
        try:
            return SafeET.fromstring(raw)
        except Exception as exc:
            if isinstance(exc, StdET.ParseError):
                raise
            raise StdET.ParseError(str(exc)) from exc
    return StdET.fromstring(raw)


def is_valid_tunnel_identifier(value: str | None) -> bool:
    """Return True if value is a safe tunnel_id / server_key slug with no PostgREST metacharacters."""
    if not value or not isinstance(value, str):
        return False
    return bool(_TUNNEL_IDENTIFIER_RE.fullmatch(value.strip()))


def sanitize_tunnel_identifier(value: str | None) -> str | None:
    """Return stripped tunnel identifier if valid, else None."""
    if not value or not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if _TUNNEL_IDENTIFIER_RE.fullmatch(cleaned) else None


def lookup_mcp_server_by_key_or_name(
    supabase_client: Any,
    identifier: str | None,
    columns: str = "*",
) -> list[dict[str, Any]]:
    """
    Safely query `mcp_servers` by `server_key` or `name` without PostgREST filter injection.
    Returns matching rows (or empty list if identifier is invalid).
    """
    safe_id = sanitize_tunnel_identifier(identifier)
    if not safe_id:
        return []
    result = (
        supabase_client.table("mcp_servers")
        .select(columns)
        .or_(f"server_key.eq.{safe_id},name.eq.{safe_id}")
        .execute()
    )
    return list(result.data or [])


def constant_time_equals(a: str | None, b: str | None) -> bool:
    """Timing-safe string comparison for secrets and API keys."""
    if not a or not b or not isinstance(a, str) or not isinstance(b, str):
        return False
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def egdesk_api_key_is_registered(api_key: str | None, supabase_client: Any = None) -> bool:
    """Return True if `api_key` matches any tunnel's `description.api_key` in Supabase."""
    if not api_key or not isinstance(api_key, str):
        return False
    client = supabase_client
    if client is None:
        url = os.getenv("SUPABASE_URL")
        service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not service_key:
            return False
        try:
            from supabase import create_client

            client = create_client(url, service_key)
        except Exception:
            return False

    try:
        result = client.table("mcp_servers").select("description").limit(500).execute()
        matched = False
        for row in result.data or []:
            try:
                stored = json.loads(row.get("description") or "{}").get("api_key")
                if isinstance(stored, str) and constant_time_equals(stored, api_key):
                    matched = True
            except Exception:
                continue
        return matched
    except Exception as exc:
        print(f"⚠️ Gateway API key lookup failed: {exc}")
        return False


def verify_gateway_request_auth(
    request: Any,
    *,
    env_keys: tuple[str, ...] = ("GATEWAY_API_KEY",),
    require_by_default: bool = False,
    supabase_client: Any = None,
) -> tuple[bool, str | None]:
    """
    Verify X-Api-Key or Authorization Bearer token on gateway utility routes.

    - Checks configured env_keys (e.g. GATEWAY_API_KEY, SHEET_SYNC_API_KEY, FEDEX_GATEWAY_API_KEY).
    - Checks registered tunnel API keys in `mcp_servers`.
    - Checks Supabase Auth Bearer tokens.
    - When `require_by_default=False`, authentication is enforced whenever any of `env_keys`
      or `REQUIRE_GATEWAY_API_KEY=true` is set, OR whenever the caller supplies credentials.
    """
    configured_env_secrets = [
        (os.getenv(k) or "").strip() for k in env_keys if (os.getenv(k) or "").strip()
    ]
    global_require = (os.getenv("REQUIRE_GATEWAY_API_KEY") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    must_authenticate = require_by_default or bool(configured_env_secrets) or global_require

    api_key = (request.headers.get("X-Api-Key") or "").strip()
    auth_header = (request.headers.get("Authorization") or "").strip()
    bearer_token = (
        auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    )

    if not api_key and not bearer_token:
        if must_authenticate:
            return False, "Authentication required (provide X-Api-Key or Authorization Bearer token)."
        return True, None

    # 1. Check explicit environment API keys
    for candidate in (api_key, bearer_token):
        if candidate:
            for secret in configured_env_secrets:
                if constant_time_equals(candidate, secret):
                    return True, None

    # 2. Check registered tunnel X-Api-Key in Supabase
    if api_key and egdesk_api_key_is_registered(api_key, supabase_client=supabase_client):
        return True, None

    # 3. Check Supabase Auth Bearer token (or bearer token matching a tunnel api_key)
    if bearer_token:
        if egdesk_api_key_is_registered(bearer_token, supabase_client=supabase_client):
            return True, None
        client = supabase_client
        if client is None:
            url = os.getenv("SUPABASE_URL")
            service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            if url and service_key:
                try:
                    from supabase import create_client

                    client = create_client(url, service_key)
                except Exception:
                    client = None
        if client is not None:
            try:
                user_resp = client.auth.get_user(bearer_token)
                if user_resp and getattr(user_resp, "user", None):
                    return True, None
            except Exception:
                pass

    return False, "Invalid API key or Bearer token."


def _is_disallowed_webhook_ip(ip_str: str, *, allow_local: bool) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True

    # Always block link-local (169.254.0.0/16, fe80::/10), unspecified, multicast, reserved
    if (
        addr.is_link_local
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
    ):
        return True

    # Alibaba / cloud metadata IP
    if ip_str == "100.100.100.200":
        return True

    if not allow_local and (addr.is_loopback or addr.is_private):
        return True

    return False


def validate_outbound_webhook_url(
    url: str,
    *,
    allow_local: bool | None = None,
    resolve_dns: bool = True,
) -> str:
    """
    Validate an outbound webhook/callback URL to prevent SSRF.
    Raises ValueError if the URL is invalid or targets internal/metadata addresses.
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("callback_url is required")

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("callback_url must use http or https scheme")

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise ValueError("callback_url must include a valid hostname")

    if hostname in _BLOCKED_WEBHOOK_HOSTS or hostname.endswith(".internal"):
        raise ValueError(f"callback_url host '{hostname}' is not permitted")

    if allow_local is None:
        # Allow localhost callbacks only when explicitly enabled or not running on Render
        env_allow = (os.getenv("ALLOW_LOCAL_SHEET_CALLBACKS") or "").strip().lower()
        if env_allow in ("1", "true", "yes"):
            allow_local = True
        elif env_allow in ("0", "false", "no"):
            allow_local = False
        else:
            allow_local = not bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))

    if not allow_local and hostname in ("localhost", "localhost.localdomain"):
        raise ValueError("Loopback callback_url is not permitted in production")

    # Direct IP literal check
    try:
        ipaddress.ip_address(hostname)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False

    if is_ip_literal:
        if _is_disallowed_webhook_ip(hostname, allow_local=allow_local):
            raise ValueError(f"callback_url IP '{hostname}' is not permitted")
        return raw

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve callback_url host '{hostname}': {exc}") from exc

        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            resolved_ip = str(sockaddr[0])
            if _is_disallowed_webhook_ip(resolved_ip, allow_local=allow_local):
                raise ValueError(
                    f"callback_url host '{hostname}' resolves to restricted address '{resolved_ip}'"
                )

    return raw


def validate_google_drive_id(value: str | None, field_name: str = "id") -> str:
    """Validate a Google Spreadsheet / Drive file / folder ID to prevent Drive query injection."""
    cleaned = (value or "").strip()
    if not cleaned or not _DRIVE_ID_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid {field_name}: must be 1-128 alphanumeric, '-', or '_' characters")
    return cleaned


_INTERNAL_PROXY_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_internal_proxy_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(addr in net for net in _INTERNAL_PROXY_NETWORKS)


def extract_trusted_client_ip(headers: Any, client_host: str | None = None) -> str:
    """
    Extract client IP safely behind Render / Cloudflare load balancers.

    - Do NOT trust client-spoofed `X-Real-IP` or the leftmost `X-Forwarded-For` entry.
    - Prefer `CF-Connecting-IP` / `True-Client-IP` if present from Cloudflare/Render edge,
      otherwise walk `X-Forwarded-For` from right to left and return the first non-internal
      (or rightmost) hop appended by the trusted proxy.
    """
    if headers is not None:
        cf_ip = (headers.get("cf-connecting-ip") or headers.get("true-client-ip") or "").strip()
        if cf_ip:
            try:
                ipaddress.ip_address(cf_ip)
                return cf_ip
            except ValueError:
                pass

        forwarded_for = (headers.get("x-forwarded-for") or "").strip()
        if forwarded_for:
            hops = [part.strip() for part in forwarded_for.split(",") if part.strip()]
            valid_hops: list[str] = []
            for hop in hops:
                try:
                    ipaddress.ip_address(hop)
                    valid_hops.append(hop)
                except ValueError:
                    continue
            # Walk right-to-left: return the rightmost external IP (added by the edge proxy),
            # or fall back to the rightmost valid IP if all hops are internal/loopback.
            for hop in reversed(valid_hops):
                addr = ipaddress.ip_address(hop)
                if not _is_internal_proxy_ip(addr):
                    return hop
            if valid_hops:
                return valid_hops[-1]

    if client_host:
        return client_host
    return "unknown"
