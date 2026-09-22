"""
Visitor Google OAuth — handled on the tunnel gateway (Render), not forwarded to EGDesk desktop.

Published sites never receive Supabase keys. OAuth start, callback completion, code exchange,
and visitor Drive/Sheets reads run here so login does not depend on a live WebSocket tunnel.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from supabase import Client

PENDING_TTL_SECONDS = 15 * 60
CODE_TTL_SECONDS = 2 * 60
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

VISITOR_IDENTITY_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]

VISITOR_WORKSPACE_SCOPES = [
    *VISITOR_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/presentations",
]

CALLBACK_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>EGDesk visitor sign-in</title></head>
<body style="font-family: system-ui, sans-serif; padding: 24px;">
  <p id="msg">Finishing Google sign-in…</p>
  <script>
(function () {
  var msg = document.getElementById('msg');
  var url = window.location.href;
  var hasHash = window.location.hash && window.location.hash.length > 1;
  var hasCode = new URLSearchParams(window.location.search).has('code');
  if (!hasHash && !hasCode) {
    msg.textContent = 'No authorization data in this page. Close this window and try Sign in with Google again.';
    return;
  }
  var finished = false;
  var timer = setTimeout(function () {
    if (finished) return;
    finished = true;
    msg.textContent = 'Sign-in timed out. Close this page and try Sign in with Google again.';
  }, 20000);
  var completePath = window.location.pathname.replace(/\\/visitor-auth\\/callback.*$/, '/visitor-auth/callback/complete');
  fetch(completePath, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: url })
  }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      if (res.ok && res.j && res.j.redirectTo) {
        msg.textContent = 'Signed in. Returning to the site…';
        window.location.replace(res.j.redirectTo);
      } else {
        msg.textContent = 'Sign-in failed: ' + ((res.j && (res.j.error || res.j.message)) || 'unknown error');
      }
    })
    .catch(function (err) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      msg.textContent = 'Could not reach EGDesk: ' + (err && err.message ? err.message : err);
    });
})();
  </script>
</body>
</html>"""


class VisitorAudienceError(Exception):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(byte_length: int = 24) -> str:
    return secrets.token_urlsafe(byte_length)


def normalize_origin(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid origin: {value}")
    return f"{parsed.scheme}://{parsed.netloc}"


def is_localhost_origin(value: str) -> bool:
    try:
        hostname = urlparse(value).hostname or ""
        return hostname in ("localhost", "127.0.0.1", "[::1]")
    except Exception:
        return False


def visitor_audience_from_return_to(return_to: str) -> str:
    return normalize_origin(return_to)


def resolve_visitor_oauth_redirect_to(
    pending_id: str,
    return_to: str,
    egdesk_public_url: str,
    local_callback_origin: str = "http://localhost:54321",
) -> str:
    if is_localhost_origin(return_to):
        base = local_callback_origin.rstrip("/")
        return f"{base}/auth/callback"
    base = egdesk_public_url.rstrip("/")
    return f"{base}/visitor-auth/callback/{pending_id}"


def pending_id_from_callback_url(url: str) -> str | None:
    parsed = urlparse(url)
    match = re.search(r"/visitor-auth/callback/([^/]+)(?:/complete)?$", parsed.path)
    if match and match.group(1) != "complete":
        return match.group(1)
    legacy = parse_qs(parsed.query).get("vid", [None])[0]
    return legacy or None


def visitor_request_origin(request: Request) -> str | None:
    for header in ("origin", "x-visitor-origin", "referer"):
        raw = request.headers.get(header)
        if not raw or raw == "null":
            continue
        try:
            return normalize_origin(raw)
        except Exception:
            continue
    return None


def assert_visitor_audience(audience: str, request_origin: str | None) -> None:
    if not request_origin:
        raise VisitorAudienceError("Missing site origin for visitor session.")
    if request_origin != audience:
        raise VisitorAudienceError("Visitor session is not valid for this site.")


def resolve_visitor_google_scopes(raw: Any = None) -> list[str]:
    if raw in (None, "", []):
        requested = list(VISITOR_WORKSPACE_SCOPES)
    elif raw == "basic":
        requested = list(VISITOR_IDENTITY_SCOPES)
    elif raw == "workspace":
        requested = list(VISITOR_WORKSPACE_SCOPES)
    elif isinstance(raw, list):
        requested = [str(item) for item in raw if item]
    elif isinstance(raw, str):
        requested = [part for part in re.split(r"[\s,]+", raw) if part]
    else:
        requested = list(VISITOR_WORKSPACE_SCOPES)

    seen: set[str] = set()
    result: list[str] = []
    for scope in [*VISITOR_IDENTITY_SCOPES, *requested]:
        if not scope or scope in seen:
            continue
        if scope != "openid" and not scope.startswith("https://"):
            continue
        seen.add(scope)
        result.append(scope)
    return result


def _supabase_anon_key() -> str:
    key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    if not key:
        raise RuntimeError("SUPABASE_ANON_KEY is required for visitor Google auth on the gateway")
    return key


def _supabase_url() -> str:
    url = os.getenv("SUPABASE_URL", "").strip()
    if not url:
        raise RuntimeError("SUPABASE_URL is required for visitor Google auth on the gateway")
    return url.rstrip("/")


async def _verify_tunnel_api_key(tunnel_id: str, request: Request, supabase_client: Client) -> bool:
    api_key_header = request.headers.get("X-Api-Key")
    if not api_key_header:
        return False
    try:
        server_check = (
            supabase_client.table("mcp_servers")
            .select("description")
            .or_(f"server_key.eq.{tunnel_id},name.eq.{tunnel_id}")
            .execute()
        )
        stored_key = None
        if server_check.data:
            try:
                desc_json = json.loads(server_check.data[0].get("description") or "{}")
                stored_key = desc_json.get("api_key")
            except Exception:
                stored_key = None
        return bool(stored_key and stored_key == api_key_header)
    except Exception:
        return False


class VisitorAuthStore:
    """Supabase-backed pending rows, one-time codes, and opaque sessions."""

    def __init__(self, supabase_client: Client):
        self.supabase = supabase_client

    def _sweep(self) -> None:
        now = _now_iso()
        for table in ("visitor_auth_pending", "visitor_auth_codes", "visitor_auth_sessions"):
            try:
                self.supabase.table(table).delete().lt("expires_at", now).execute()
            except Exception as exc:
                print(f"[visitor-auth] sweep {table} failed: {exc}")

    def save_pending(
        self,
        pending_id: str,
        tunnel_id: str,
        return_to: str,
        audience: str,
        scopes: list[str],
    ) -> None:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=PENDING_TTL_SECONDS)).isoformat()
        self.supabase.table("visitor_auth_pending").upsert(
            {
                "id": pending_id,
                "tunnel_id": tunnel_id,
                "return_to": return_to,
                "audience": audience,
                "scopes": scopes,
                "created_at": _now_iso(),
                "expires_at": expires_at,
            }
        ).execute()

    def get_pending(self, pending_id: str) -> dict[str, Any] | None:
        self._sweep()
        result = self.supabase.table("visitor_auth_pending").select("*").eq("id", pending_id).limit(1).execute()
        rows = result.data or []
        return rows[0] if rows else None

    def delete_pending(self, pending_id: str) -> None:
        self.supabase.table("visitor_auth_pending").delete().eq("id", pending_id).execute()

    def localhost_pending_id(self, tunnel_id: str) -> str | None:
        """Match newest localhost pending when OAuth bounced to /auth/callback."""
        self._sweep()
        result = (
            self.supabase.table("visitor_auth_pending")
            .select("id, return_to, created_at")
            .eq("tunnel_id", tunnel_id)
            .order("created_at", desc=True)
            .execute()
        )
        for row in result.data or []:
            if is_localhost_origin(row.get("return_to") or ""):
                return row["id"]
        return None

    def save_session(self, session: dict[str, Any]) -> None:
        self.supabase.table("visitor_auth_sessions").upsert(session).execute()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        self._sweep()
        result = (
            self.supabase.table("visitor_auth_sessions")
            .select("*")
            .eq("session_id", session_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    def delete_session(self, session_id: str) -> None:
        self.supabase.table("visitor_auth_sessions").delete().eq("session_id", session_id).execute()

    def save_code(self, code: str, session_id: str) -> None:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=CODE_TTL_SECONDS)).isoformat()
        self.supabase.table("visitor_auth_codes").upsert(
            {"code": code, "session_id": session_id, "expires_at": expires_at}
        ).execute()

    def pop_code(self, code: str) -> dict[str, Any] | None:
        self._sweep()
        result = self.supabase.table("visitor_auth_codes").select("*").eq("code", code).limit(1).execute()
        rows = result.data or []
        if not rows:
            return None
        row = rows[0]
        self.supabase.table("visitor_auth_codes").delete().eq("code", code).execute()
        return row


class VisitorAuthService:
    def __init__(self, supabase_client: Client):
        self.supabase = supabase_client
        self.store = VisitorAuthStore(supabase_client)
        self.anon_key = _supabase_anon_key()
        self.supabase_url = _supabase_url()

    async def start_google_login(
        self,
        *,
        tunnel_id: str,
        return_to: str,
        egdesk_public_url: str,
        request_origin: str | None,
        scopes: Any = None,
        force_consent: bool = False,
    ) -> dict[str, Any]:
        audience = visitor_audience_from_return_to(return_to)
        if request_origin and request_origin != audience:
            raise ValueError("returnTo origin must match the site that started login.")

        resolved_scopes = resolve_visitor_google_scopes(scopes)
        pending_id = new_id(18)
        redirect_to = resolve_visitor_oauth_redirect_to(pending_id, return_to, egdesk_public_url)

        self.store.save_pending(pending_id, tunnel_id, return_to, audience, resolved_scopes)

        query_parts = [
            f"provider={quote('google')}",
            f"redirect_to={quote(redirect_to)}",
            f"scopes={quote(' '.join(resolved_scopes))}",
            f"access_type={quote('offline')}",
            f"prompt={quote('consent' if force_consent else 'select_account')}",
        ]
        auth_url = f"{self.supabase_url}/auth/v1/authorize?{'&'.join(query_parts)}"

        print(f"[visitor-auth] OAuth redirectTo: {redirect_to} returnTo: {return_to}")
        return {
            "authUrl": auth_url,
            "redirectUri": redirect_to,
            "audience": audience,
            "scopes": resolved_scopes,
        }

    async def complete_callback(self, browser_url: str, tunnel_id: str) -> dict[str, str]:
        pending_id = pending_id_from_callback_url(browser_url)
        if not pending_id:
            parsed = urlparse(browser_url)
            path = parsed.path.rstrip("/") or "/"
            if path == "/auth/callback":
                pending_id = self.store.localhost_pending_id(tunnel_id)
        if not pending_id:
            raise ValueError("Missing visitor login id. Close this page and try Sign in with Google again.")

        pending = self.store.get_pending(pending_id)
        if not pending:
            raise ValueError("Visitor login expired. Try Sign in with Google again.")

        parsed = urlparse(browser_url)
        hash_params = parse_qs(parsed.fragment)
        query_params = parse_qs(parsed.query)

        access_token = (hash_params.get("access_token") or [None])[0]
        refresh_token = (hash_params.get("refresh_token") or [""])[0]
        provider_token = (hash_params.get("provider_token") or [None])[0]
        provider_refresh_token = (hash_params.get("provider_refresh_token") or [None])[0]
        auth_code = (query_params.get("code") or [None])[0]

        session_payload: dict[str, Any] | None = None

        async with httpx.AsyncClient(timeout=30.0) as client:
            if access_token:
                user_response = await client.get(
                    f"{self.supabase_url}/auth/v1/user",
                    headers={"apikey": self.anon_key, "Authorization": f"Bearer {access_token}"},
                )
                if user_response.status_code >= 400:
                    raise RuntimeError("Failed to establish visitor session")
                session_payload = {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "user": user_response.json(),
                    "provider_token": provider_token,
                    "provider_refresh_token": provider_refresh_token,
                }
            elif auth_code:
                response = await client.post(
                    f"{self.supabase_url}/auth/v1/token?grant_type=pkce",
                    headers={"apikey": self.anon_key, "Content-Type": "application/json"},
                    json={"auth_code": auth_code},
                )
                data = response.json() if response.content else {}
                if response.status_code >= 400 or not data.get("access_token"):
                    raise RuntimeError(data.get("msg") or data.get("error_description") or "Failed to exchange visitor auth code")
                session_payload = data
            else:
                raise ValueError("No authorization data in callback")

        user = session_payload.get("user") or {}
        user_id = user.get("id")
        if not user_id:
            raise ValueError("Visitor session has no user id")

        google_access = session_payload.get("provider_token") or ""
        google_refresh = session_payload.get("provider_refresh_token")
        if not google_access and not google_refresh:
            raise ValueError("Google did not return a provider token. Try signing in again with consent.")

        google_expires_at = int(time.time()) + 3600
        email = user.get("email")

        self.supabase.table("user_google_tokens").upsert(
            {
                "user_id": user_id,
                "provider": "google",
                "access_token": google_access or None,
                "refresh_token": google_refresh,
                "expires_at": datetime.fromtimestamp(google_expires_at, tz=timezone.utc).isoformat(),
                "scopes": pending.get("scopes") or [],
                "provider_email": email,
                "is_active": True,
            },
            on_conflict="user_id,provider",
        ).execute()

        session_id = new_id(32)
        session_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat()
        self.store.save_session(
            {
                "session_id": session_id,
                "tunnel_id": pending.get("tunnel_id"),
                "user_id": user_id,
                "email": email,
                "audience": pending.get("audience"),
                "supabase_access_token": session_payload.get("access_token"),
                "supabase_refresh_token": session_payload.get("refresh_token") or "",
                "google_access_token": google_access,
                "google_refresh_token": google_refresh,
                "google_expires_at": google_expires_at,
                "scopes": pending.get("scopes") or [],
                "created_at": _now_iso(),
                "expires_at": session_expires_at,
            }
        )

        code_id = new_id(24)
        self.store.save_code(code_id, session_id)
        self.store.delete_pending(pending_id)

        redirect = urlparse(pending["return_to"])
        query = parse_qs(redirect.query)
        query["code"] = [code_id]
        redirect_to = urlunparse(
            (
                redirect.scheme,
                redirect.netloc,
                redirect.path,
                redirect.params,
                urlencode(query, doseq=True),
                redirect.fragment,
            )
        )
        return {"redirectTo": redirect_to}

    def exchange_code(self, code: str, request_origin: str | None) -> dict[str, Any]:
        row = self.store.pop_code(code)
        if not row:
            raise ValueError("Invalid or expired visitor login code")
        session = self.store.get_session(row["session_id"])
        if not session:
            raise ValueError("Visitor session expired. Sign in again.")
        assert_visitor_audience(session["audience"], request_origin)
        return {
            "sessionId": session["session_id"],
            "email": session.get("email"),
            "userId": session.get("user_id"),
            "audience": session.get("audience"),
        }

    def logout(self, session_id: str | None, request_origin: str | None) -> None:
        session = self.store.get_session(session_id or "")
        if not session:
            return
        assert_visitor_audience(session["audience"], request_origin)
        self.store.delete_session(session["session_id"])

    def get_status(self, session_id: str | None, request_origin: str | None) -> dict[str, Any]:
        session = self.store.get_session(session_id or "")
        if not session:
            return {
                "connected": False,
                "email": None,
                "userId": None,
                "audience": None,
                "message": "Visitor is not signed in.",
            }
        assert_visitor_audience(session["audience"], request_origin)
        return {
            "connected": bool(session.get("google_access_token") or session.get("google_refresh_token")),
            "email": session.get("email"),
            "userId": session.get("user_id"),
            "audience": session.get("audience"),
            "message": "Visitor Google is connected.",
        }

    async def _google_access_token(self, session: dict[str, Any]) -> str:
        expires_at = int(session.get("google_expires_at") or 0)
        if session.get("google_access_token") and expires_at * 1000 - 5 * 60 * 1000 > _now_ms():
            return session["google_access_token"]

        jwt = session.get("supabase_access_token") or ""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.supabase_url}/functions/v1/refresh-google-token",
                headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
            )
            data = response.json() if response.content else {}
            if response.status_code >= 400 or not data.get("access_token"):
                if session.get("google_access_token"):
                    return session["google_access_token"]
                raise ValueError("Google token expired. Sign in with Google again.")

        session["google_access_token"] = data["access_token"]
        if data.get("refresh_token"):
            session["google_refresh_token"] = data["refresh_token"]
        if data.get("expires_at"):
            session["google_expires_at"] = int(
                datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            )
        else:
            session["google_expires_at"] = int(time.time()) + 3600
        self.store.save_session(session)
        return session["google_access_token"]

    async def list_drive_files(
        self,
        session_id: str,
        request_origin: str | None,
        page_size: int = 20,
        query: str = "trashed = false",
    ) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError("Visitor is not signed in. Call startVisitorGoogleLogin() first.")
        assert_visitor_audience(session["audience"], request_origin)
        access_token = await self._google_access_token(session)
        params = {
            "pageSize": str(min(page_size, 100)),
            "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
            "q": query or "trashed = false",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            data = response.json() if response.content else {}
            if response.status_code >= 400:
                raise RuntimeError((data.get("error") or {}).get("message") or "Drive list failed")
        return {"files": data.get("files") or [], "email": session.get("email")}

    async def get_sheet_range(
        self,
        session_id: str,
        spreadsheet_id: str,
        range_a1: str,
        request_origin: str | None,
    ) -> dict[str, Any]:
        if not spreadsheet_id or not range_a1:
            raise ValueError("spreadsheetId and range are required")
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError("Visitor is not signed in. Call startVisitorGoogleLogin() first.")
        assert_visitor_audience(session["audience"], request_origin)
        access_token = await self._google_access_token(session)
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(range_a1, safe='')}"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            data = response.json() if response.content else {}
            if response.status_code >= 400:
                raise RuntimeError((data.get("error") or {}).get("message") or "Sheets read failed")
        return {
            "spreadsheetId": spreadsheet_id,
            "range": data.get("range") or range_a1,
            "values": data.get("values") or [],
        }


def _session_id_from_request(request: Request, body: dict[str, Any]) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    session_id = body.get("sessionId")
    return session_id if isinstance(session_id, str) and session_id else None


def _error_status(message: str) -> int:
    if re.search(
        r"not signed in|expired|Invalid or expired|Missing visitor|not valid for this site|Missing site origin|returnTo origin",
        message,
        re.I,
    ):
        return 401
    return 500


async def handle_visitor_auth_http(
    tunnel_id: str,
    path: str,
    request: Request,
    supabase_client: Client,
) -> JSONResponse | HTMLResponse:
    service = VisitorAuthService(supabase_client)
    origin = visitor_request_origin(request)

    try:
        if path == "visitor-auth/callback" or (
            path.startswith("visitor-auth/callback/") and not path.endswith("/complete")
        ):
            if request.method != "GET":
                return JSONResponse(status_code=405, content={"success": False, "error": "Method not allowed"})
            print(f"🔐 Visitor OAuth callback page for tunnel {tunnel_id}")
            return HTMLResponse(content=CALLBACK_HTML, status_code=200)

        if path == "visitor-auth/callback/complete" or path.startswith("visitor-auth/callback/") and path.endswith(
            "/complete"
        ):
            if request.method != "POST":
                return JSONResponse(status_code=405, content={"success": False, "error": "Method not allowed"})
            body = await request.json()
            browser_url = body.get("url") if isinstance(body, dict) else None
            if not isinstance(browser_url, str) or not browser_url:
                return JSONResponse(status_code=400, content={"success": False, "error": "Missing url"})
            result = await service.complete_callback(browser_url, tunnel_id)
            return JSONResponse(status_code=200, content={"success": True, **result})

        if path == "visitor-auth/tools/call":
            if request.method != "POST":
                return JSONResponse(status_code=405, content={"success": False, "error": "Method not allowed"})
            if not await _verify_tunnel_api_key(tunnel_id, request, supabase_client):
                return JSONResponse(status_code=401, content={"success": False, "error": "Invalid API key"})

            body = await request.json()
            tool = str(body.get("tool") or body.get("op") or "")
            args = body.get("arguments") if isinstance(body.get("arguments"), dict) else body

            if tool == "start":
                started = await service.start_google_login(
                    tunnel_id=tunnel_id,
                    return_to=str(args.get("returnTo") or ""),
                    egdesk_public_url=str(args.get("egdeskPublicUrl") or f"https://tunneling-service.onrender.com/t/{tunnel_id}"),
                    request_origin=origin,
                    scopes=args.get("scopes"),
                    force_consent=args.get("forceConsent") is True,
                )
                return JSONResponse(status_code=200, content={"success": True, **started})

            if tool == "exchange":
                code = str(args.get("code") or "")
                if not code:
                    return JSONResponse(status_code=400, content={"success": False, "error": "Missing code"})
                exchanged = service.exchange_code(code, origin)
                return JSONResponse(status_code=200, content={"success": True, **exchanged})

            session_id = _session_id_from_request(request, args if isinstance(args, dict) else {})

            if tool == "status":
                status = service.get_status(session_id, origin)
                return JSONResponse(status_code=200, content={"success": True, **status})

            if tool == "logout":
                service.logout(session_id, origin)
                return JSONResponse(status_code=200, content={"success": True})

            return JSONResponse(
                status_code=400,
                content={"success": False, "error": f"Unknown visitor-auth tool: {tool or '(missing)'}"},
            )

        if path == "visitor-google/tools/call":
            if request.method != "POST":
                return JSONResponse(status_code=405, content={"success": False, "error": "Method not allowed"})
            if not await _verify_tunnel_api_key(tunnel_id, request, supabase_client):
                return JSONResponse(status_code=401, content={"success": False, "error": "Invalid API key"})

            body = await request.json()
            tool = str(body.get("tool") or body.get("op") or "")
            args = body.get("arguments") if isinstance(body.get("arguments"), dict) else body
            session_id = _session_id_from_request(request, args if isinstance(args, dict) else {})
            if not session_id:
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Missing visitor session (Authorization Bearer)"},
                )

            if tool == "status":
                status = service.get_status(session_id, origin)
                return JSONResponse(status_code=200, content={"success": True, **status})

            if tool == "listDriveFiles":
                result = await service.list_drive_files(
                    session_id,
                    origin,
                    page_size=int(args.get("pageSize") or 20),
                    query=str(args.get("query") or "trashed = false"),
                )
                return JSONResponse(status_code=200, content={"success": True, **result})

            if tool == "getSheetRange":
                result = await service.get_sheet_range(
                    session_id,
                    str(args.get("spreadsheetId") or ""),
                    str(args.get("range") or ""),
                    origin,
                )
                return JSONResponse(status_code=200, content={"success": True, **result})

            return JSONResponse(
                status_code=400,
                content={"success": False, "error": f"Unknown visitor-google tool: {tool or '(missing)'}"},
            )

        return JSONResponse(status_code=404, content={"success": False, "error": "Visitor auth endpoint not found"})
    except VisitorAudienceError as exc:
        return JSONResponse(status_code=401, content={"success": False, "error": str(exc)})
    except Exception as exc:
        message = str(exc)
        print(f"[visitor-auth] {message}")
        return JSONResponse(status_code=_error_status(message), content={"success": False, "error": message})
