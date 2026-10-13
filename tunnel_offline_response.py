"""HTML/JSON responses when a tunnel has no connected EGDesk desktop client."""

from __future__ import annotations

import html
import os
from typing import Any, Protocol


class _OfflineRequest(Protocol):
    headers: Any
    method: str


def wants_html_response(request: _OfflineRequest) -> bool:
    """Browsers typically send Accept: text/html; APIs and fetch() often do not."""
    if request.method not in ("GET", "HEAD"):
        return False
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _offline_redirect_url() -> str | None:
    raw = (os.getenv("TUNNEL_OFFLINE_REDIRECT_URL") or "").strip()
    return raw or None


def tunnel_offline_html(
    *,
    host: str | None = None,
    tunnel_id: str | None = None,
    kind: str = "site",
) -> str:
    """kind: 'site' (default page visit) or 'signin' (BYO OAuth callback)."""
    display_host = html.escape(host or "this site")
    if kind == "signin":
        title = "Sign-in unavailable"
        lead = (
            "Sign-in is temporarily unavailable because the operator's computer "
            "is not connected to EGDesk."
        )
        detail = (
            "Ask them to open EGDesk and start the tunnel, then try signing in again."
        )
    else:
        title = "Server not available"
        lead = f"The server behind <strong>{display_host}</strong> is not running right now."
        detail = (
            "The site owner needs to open EGDesk on their computer and connect the tunnel. "
            "This page will refresh automatically every 30 seconds."
        )

    tunnel_line = ""
    if tunnel_id:
        tunnel_line = f'<p class="meta">Tunnel: <code>{html.escape(tunnel_id)}</code></p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: #f4f4f5; color: #18181b;
    }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #09090b; color: #fafafa; }}
      .card {{ background: #18181b; border-color: #3f3f46; }}
      .meta {{ color: #a1a1aa; }}
    }}
    .card {{
      max-width: 28rem; padding: 1.75rem 2rem; margin: 1rem;
      background: #fff; border: 1px solid #e4e4e7; border-radius: 12px;
      box-shadow: 0 1px 3px rgba(0,0,0,.08);
    }}
    h1 {{ font-size: 1.25rem; margin: 0 0 .75rem; font-weight: 600; }}
    p {{ margin: 0 0 .75rem; line-height: 1.5; }}
    .meta {{ font-size: .875rem; color: #71717a; margin-top: 1rem; }}
    code {{ font-size: .8em; }}
  </style>
</head>
<body>
  <main class="card">
    <h1>{html.escape(title)}</h1>
    <p>{lead}</p>
    <p>{detail}</p>
    {tunnel_line}
  </main>
</body>
</html>"""


def tunnel_offline_json(
    *,
    host: str | None = None,
    tunnel_id: str | None = None,
    kind: str = "site",
) -> dict[str, Any]:
    if kind == "signin":
        message = (
            "Sign-in unavailable: EGDesk tunnel is offline. "
            "Operator must connect the tunnel and retry."
        )
        error = "tunnel_offline_signin"
    else:
        label = host or tunnel_id or "tunnel"
        message = f"Server for '{label}' is offline or not connected."
        error = "tunnel_offline"
    payload: dict[str, Any] = {
        "error": error,
        "message": message,
        "online": False,
        "retryable": True,
    }
    if host:
        payload["host"] = host
    if tunnel_id:
        payload["tunnel_id"] = tunnel_id
    return payload


def tunnel_offline_response(
    request: _OfflineRequest,
    *,
    tunnel_id: str | None = None,
    host: str | None = None,
    kind: str = "site",
):
    """
    Respond with 503. Browsers get HTML (or optional redirect); APIs get JSON.
    """
    redirect = _offline_redirect_url()
    if redirect and wants_html_response(request):
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url=redirect, status_code=302)

    if wants_html_response(request):
        from fastapi.responses import HTMLResponse

        return HTMLResponse(
            content=tunnel_offline_html(host=host, tunnel_id=tunnel_id, kind=kind),
            status_code=503,
            headers={"Retry-After": "30"},
        )

    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=503,
        content=tunnel_offline_json(host=host, tunnel_id=tunnel_id, kind=kind),
        headers={"Retry-After": "30"},
    )


def byo_tunnel_offline_response(request: _OfflineRequest | None = None):
    """BYO visitor OAuth callback when desktop tunnel is down."""
    if request is None:
        from fastapi.responses import HTMLResponse

        return HTMLResponse(
            content=tunnel_offline_html(kind="signin"),
            status_code=503,
        )
    return tunnel_offline_response(request, kind="signin")
