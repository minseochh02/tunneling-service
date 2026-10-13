"""BYO visitor OAuth callback routing on the tunnel gateway (D24, D26).

Platform login uses /visitor-auth/callback/{pendingId} on the gateway (Supabase).
BYO uses /visitor-auth/callback?code&state with no path segment — forward GET to desktop MCP.
"""

from __future__ import annotations

from typing import Any, Protocol


class _VisitorCallbackRequest(Protocol):
    method: str
    query_params: Any

def is_direct_oauth_callback_query(request: _VisitorCallbackRequest) -> bool:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    return bool(code and state)


def should_forward_byo_callback_to_desktop(path: str, request: _VisitorCallbackRequest) -> bool:
    """GET /visitor-auth/callback?code&state — no Supabase; forward to tunnel PC."""
    normalized = (path or "").lstrip("/")
    if normalized != "visitor-auth/callback":
        return False
    if request.method != "GET":
        return False
    return is_direct_oauth_callback_query(request)


def visitor_auth_handled_on_gateway(path: str, request: _VisitorCallbackRequest) -> bool:
    """True when this path must be handled by visitor_auth_router (platform / tools)."""
    normalized = (path or "").lstrip("/")
    if normalized in ("visitor-auth/tools/call", "visitor-google/tools/call"):
        return True
    if should_forward_byo_callback_to_desktop(normalized, request):
        return False
    if normalized == "visitor-auth/callback":
        # Bare path without BYO query — not platform (platform uses /callback/{id})
        return request.method != "GET" or not is_direct_oauth_callback_query(request)
    if normalized.startswith("visitor-auth/callback/"):
        return True
    return False


def log_visitor_callback_path_only(path: str, tunnel_id: str, method: str) -> None:
    """QA 45 — log path only, never query string."""
    print(f"🔐 Visitor OAuth path={path} method={method} tunnel={tunnel_id}")


