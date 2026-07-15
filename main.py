from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Cookie
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import uuid
import os
import hashlib
import secrets
import time
import httpx
from datetime import datetime, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client
from sheet_sync_router import sheet_sync_router

# Load environment variables
load_dotenv()

# Initialize Supabase client
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
RENDER_API_KEY = os.getenv("RENDER_API_KEY")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI()

# After your app is created, include the router

app.include_router(sheet_sync_router)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # Next.js dev server
        "http://localhost:3001",
        "https://egdesk.cloud",  # Production custom domain
        "https://www.egdesk.cloud",  # Production with www
        "https://egdesk-website.vercel.app",  # Vercel production
        "https://egdesk-website-git-main-minseochhs-projects.vercel.app",  # Vercel preview
    ],
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods (GET, POST, PUT, DELETE, etc.)
    allow_headers=["*"],  # Allow all headers including Authorization
    expose_headers=["Set-Cookie"],  # Allow browser to see Set-Cookie header
)

# Store active tunnel connections
active_tunnels = {}  # {tunnel_id: websocket}
# tunnel_api_keys = {}  # REMOVED: Now stored in Supabase 'mcp_servers' table
pending_requests = {}  # {request_id: asyncio.Future}
streaming_requests = {}  # {request_id: asyncio.Queue} for SSE streaming

# Session storage for authenticated iframe access
# {session_token: {user_id, user_email, tunnel_id, created_at, expires_at}}
iframe_sessions = {}

# Public access flags — per-project access control
# Populated from mcp_servers.description when tunnel connects
# Format: {tunnel_id: {"__default__": bool, "project_name": bool, ...}}
tunnel_public_flags = {}  # {tunnel_id: {str: bool}}

# Custom-domain routing cache. Maps hostnames to tunnel IDs to avoid hitting
# Supabase on every request for user domains like www.example.com.
domain_route_cache = {}  # {domain: (tunnel_id, timestamp)}
DOMAIN_ROUTE_CACHE_TTL_SECONDS = 300
PRIMARY_DOMAINS = {
    "tunneling-service.onrender.com",
    "domains.egdesk.cloud",
    "egdesk.cloud",
    "www.egdesk.cloud",
    "localhost",
    "127.0.0.1",
}

def verify_ip_ownership(client_ip: str, stored_ip_hash: str, stored_salt: str = None) -> bool:
    """
    LEGACY: Verify that the client IP matches the stored owner IP.
    Supports both legacy (plain IP) and new (hashed IP with salt) formats.
    This is kept for backward compatibility with servers that don't have owner_user_id.
    """
    if not stored_ip_hash:
        return False
    if not stored_salt:
        # Legacy: direct comparison
        return stored_ip_hash == client_ip
    else:
        # New: hash the client IP with the stored salt and compare
        combined = (client_ip + stored_salt).encode('utf-8')
        computed_hash = hashlib.sha256(combined).hexdigest()
        return computed_hash == stored_ip_hash


def get_user_from_auth_header(request: Request) -> tuple[str | None, str | None]:
    """
    Extract user ID and email from Authorization header.
    Returns (user_id, user_email) or (None, None) if not authenticated.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None, None
    
    token = auth_header.replace("Bearer ", "")
    try:
        user_response = supabase.auth.get_user(token)
        if user_response and user_response.user:
            return user_response.user.id, user_response.user.email
    except Exception as e:
        print(f"⚠️ Failed to get user from token: {e}")
    
    return None, None


def get_client_ip(request: Request) -> str:
    """Extract client IP from request headers."""
    forwarded_for = request.headers.get("x-forwarded-for")
    real_ip = request.headers.get("x-real-ip")
    client_host = request.client.host if request.client else None
    return real_ip or (forwarded_for.split(',')[0].strip() if forwarded_for else None) or client_host or "unknown"


def normalize_host(host: str | None) -> str:
    """Normalize a Host header value to a lowercase hostname without port."""
    return (host or "").lower().split(":")[0].strip().rstrip(".")


def parse_description_json(raw_description) -> dict:
    """Parse mcp_servers.description while tolerating legacy plain text values."""
    if isinstance(raw_description, dict):
        return raw_description
    if not raw_description:
        return {}
    if not isinstance(raw_description, str):
        return {}
    try:
        parsed = json.loads(raw_description)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def extract_project_from_path(path: str) -> str | None:
    """Extract project name from a /p/{project_name}/... path."""
    if path.startswith("p/"):
        parts = path.split("/", 2)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return None


def is_project_public(tunnel_id: str, project_name: str | None = None) -> bool:
    """Check if a specific project (or the tunnel default) is public."""
    flags = tunnel_public_flags.get(tunnel_id)
    if flags is None:
        return False
    # Backward compat: old code stored a bare bool
    if isinstance(flags, bool):
        return flags
    if project_name:
        # Per-project flag takes priority, fall back to tunnel default
        if project_name in flags:
            return flags[project_name]
        # Case-insensitive fallback
        lower = project_name.lower()
        for key, val in flags.items():
            if key != "__default__" and key.lower() == lower:
                return val
    return flags.get("__default__", False)


def load_flags_from_description(desc_json: dict) -> dict:
    """Build a flags dict from a parsed description JSON."""
    default_public = bool(desc_json.get("public_access", False))
    project_access = desc_json.get("project_access") or {}
    return {"__default__": default_public, **{k: bool(v) for k, v in project_access.items()}}


def clear_domain_route_cache(domain: str | None = None):
    if domain:
        domain_route_cache.pop(domain, None)
    else:
        domain_route_cache.clear()


def is_synthetic_owner_permission_id(permission_id) -> bool:
    """True when permission_id is the owner sentinel from session_exchange(), not a DB UUID."""
    return isinstance(permission_id, str) and permission_id.startswith("owner-")


def track_connection_session(
    *,
    server_id: str,
    user_id: str,
    permission_id: str,
    session_token: str | None = None,
) -> None:
    """
    Best-effort analytics/revocation bookkeeping for permission-based sessions.
    Skips owner sentinel IDs (not valid UUIDs in mcp_connection_sessions.permission_id).
    Never raises — connection tracking must not block request serving.
    """
    if is_synthetic_owner_permission_id(permission_id):
        return

    try:
        existing_session = (
            supabase.table("mcp_connection_sessions")
            .select("*")
            .eq("permission_id", permission_id)
            .eq("status", "active")
            .execute()
        )

        if existing_session.data and len(existing_session.data) > 0:
            session_id = existing_session.data[0]["id"]
            supabase.table("mcp_connection_sessions").update({
                "last_activity_at": datetime.utcnow().isoformat(),
                "requests_count": existing_session.data[0]["requests_count"] + 1,
            }).eq("id", session_id).execute()
        else:
            token_value = session_token or secrets.token_urlsafe(32)
            supabase.table("mcp_connection_sessions").insert({
                "server_id": server_id,
                "user_id": user_id,
                "permission_id": permission_id,
                "session_token": token_value[:16] + "...",
                "status": "active",
                "connected_at": datetime.utcnow().isoformat(),
                "last_activity_at": datetime.utcnow().isoformat(),
                "requests_count": 1,
            }).execute()
    except Exception as e:
        print(f"⚠️ Failed to update connection session tracking: {e}")


async def resolve_domain_to_tunnel(domain: str) -> str | None:
    """Resolve a configured custom domain to a tunnel/server key."""
    domain = normalize_host(domain)
    if not domain or domain in PRIMARY_DOMAINS:
        return None

    cached = domain_route_cache.get(domain)
    if cached:
        tunnel_id, timestamp = cached
        if time.time() - timestamp < DOMAIN_ROUTE_CACHE_TTL_SECONDS:
            return tunnel_id
        domain_route_cache.pop(domain, None)

    try:
        result = supabase.table("mcp_servers") \
            .select("server_key, name, description") \
            .ilike("description", f"%{domain}%") \
            .execute()

        for row in result.data or []:
            desc = parse_description_json(row.get("description"))
            custom_domains = desc.get("custom_domains") or []
            normalized_domains = {normalize_host(str(item)) for item in custom_domains}
            if domain in normalized_domains:
                # Prefer name (slug) over server_key since active_tunnels is keyed by name
                tunnel_id = row.get("name") or row.get("server_key")
                if tunnel_id:
                    domain_route_cache[domain] = (tunnel_id, time.time())
                    print(f"🌐 Custom domain resolved: {domain} → tunnel_id='{tunnel_id}' (name='{row.get('name')}', server_key='{row.get('server_key')}')")
                    return tunnel_id
    except Exception as e:
        print(f"❌ Domain lookup failed for {domain}: {e}")

    return None


def normalize_custom_domains(domains) -> list[str]:
    """Normalize and de-duplicate custom domain hostnames from API input."""
    normalized = []
    for domain in domains or []:
        host = normalize_host(str(domain).replace("https://", "").replace("http://", "").split("/")[0])
        if not host or host in PRIMARY_DOMAINS or "." not in host:
            continue
        if host not in normalized:
            normalized.append(host)
    return normalized


async def add_custom_domain_to_render(domain: str) -> dict:
    """Register a custom domain on the Render service so Render can issue SSL."""
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return {"domain": domain, "success": False, "skipped": True, "error": "RENDER_API_KEY or RENDER_SERVICE_ID is not configured"}

    url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/custom-domains"
    headers = {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json={"name": domain})

        body = None
        try:
            body = response.json()
        except Exception:
            body = response.text

        # Render may return a conflict/client error if the domain already exists.
        # Treat text containing "already" as non-fatal so retries are idempotent.
        if response.status_code in (200, 201, 202):
            return {"domain": domain, "success": True, "status_code": response.status_code, "response": body}
        if response.status_code in (400, 409) and "already" in str(body).lower():
            return {"domain": domain, "success": True, "already_exists": True, "status_code": response.status_code, "response": body}

        return {"domain": domain, "success": False, "status_code": response.status_code, "error": body}
    except Exception as e:
        return {"domain": domain, "success": False, "error": str(e)}


def merge_custom_domains_description(raw_description, domains: list[str]) -> str:
    desc = parse_description_json(raw_description)
    existing = desc.get("custom_domains") or []
    merged = []
    for domain in [*existing, *domains]:
        host = normalize_host(str(domain))
        if host and host not in merged:
            merged.append(host)
    desc["custom_domains"] = merged
    desc["custom_domain_updated_at"] = datetime.utcnow().isoformat()
    return json.dumps(desc)


def verify_ownership(request: Request, server_data: dict) -> tuple[bool, str | None]:
    """
    Verify server ownership using User ID (preferred) or IP (legacy fallback).
    Returns (is_owner, error_message).
    
    Priority:
    1. User ID verification (if owner_user_id is set and user is authenticated)
    2. IP verification (legacy fallback for servers without owner_user_id)
    """
    # Try User ID verification first (preferred method)
    user_id, user_email = get_user_from_auth_header(request)
    stored_owner_id = server_data.get("owner_user_id")
    
    if stored_owner_id:
        # Server has User ID-based ownership
        if not user_id:
            return False, "Authentication required. Please include a valid Bearer token."
        if user_id == stored_owner_id:
            print(f"✅ Ownership verified via User ID for {user_email}")
            return True, None
        else:
            return False, "You are not the owner of this server"
    
    # Fallback to IP verification for legacy servers (no owner_user_id)
    client_ip = get_client_ip(request)
    stored_ip_hash = server_data.get("owner_ip")
    stored_salt = server_data.get("owner_ip_salt")
    
    if verify_ip_ownership(client_ip, stored_ip_hash, stored_salt):
        print(f"✅ Ownership verified via IP (legacy) for {client_ip}")
        return True, None
    
    return False, "Your IP does not match the server owner's IP. If you're the owner, please re-register to update ownership."

async def route_custom_domain_request(path: str, request: Request):
    """Route a request whose Host header is a configured custom domain."""
    host = normalize_host(request.headers.get("host"))

    if not host or host in PRIMARY_DOMAINS:
        return None

    tunnel_id = await resolve_domain_to_tunnel(host)
    if not tunnel_id:
        return JSONResponse(
            status_code=404,
            content={"error": f"Domain '{host}' is not configured"},
        )

    if tunnel_id not in active_tunnels:
        print(f"❌ Custom domain '{host}' resolved to tunnel '{tunnel_id}', but it is not in active_tunnels. Active tunnels: {list(active_tunnels.keys())}")
        return JSONResponse(
            status_code=503,
            content={"error": f"Server for '{host}' is offline"},
        )

    # Refresh per-project access flags from DB for custom domain requests
    # (in-memory flags may be stale if access mode changed after tunnel connected)
    domain_project_map: dict = {}
    try:
        server_result = supabase.table("mcp_servers").select("description").or_(
            f"server_key.eq.{tunnel_id},name.eq.{tunnel_id}"
        ).execute()
        if server_result.data:
            desc = parse_description_json(server_result.data[0].get("description"))
            tunnel_public_flags[tunnel_id] = load_flags_from_description(desc)
            domain_project_map = desc.get("domain_project_map") or {}
    except Exception as e:
        print(f"⚠️ Could not load access flags for {tunnel_id}: {e}")

    # A custom domain's path (e.g. "/dashboard") never carries a "/p/{project}/" prefix
    # the way direct tunnel URLs do, so extract_project_from_path() alone can never resolve
    # which project this domain serves — it would always fall back to the tunnel-wide
    # default and ignore whatever per-project public/private toggle the user actually set.
    # domain_project_map (synced from the local domain→project mapping) is the real source
    # of truth here; only fall back to path-extraction if this domain has no mapping saved.
    request_project = domain_project_map.get(host) or extract_project_from_path(path)
    is_public = is_project_public(tunnel_id, request_project)

    # ============================================
    # Custom domain session exchange callback
    # When egdesk.cloud redirects back with ?__egdesk_session=<token>,
    # validate the session, set a cookie on this custom domain, and redirect to clean URL.
    # ============================================
    session_param = request.query_params.get("__egdesk_session")
    if session_param and session_param in iframe_sessions:
        session_data = iframe_sessions[session_param]
        # Verify session belongs to this tunnel
        if session_data.get("tunnel_id") == tunnel_id:
            # Build the clean redirect URL (strip __egdesk_session param)
            from urllib.parse import urlencode, parse_qs, urlparse
            raw_host = request.headers.get("host", host)
            scheme = "https"
            clean_path = f"/{path}" if path else "/"
            # Preserve other query params
            other_params = {k: v for k, v in request.query_params.items() if k != "__egdesk_session"}
            qs = f"?{urlencode(other_params)}" if other_params else ""
            clean_url = f"{scheme}://{raw_host}{clean_path}{qs}"

            from fastapi.responses import RedirectResponse
            response = RedirectResponse(url=clean_url, status_code=302)
            response.set_cookie(
                key=f"egdesk_session_{tunnel_id}",
                value=session_param,
                httponly=True,
                secure=True,
                samesite="lax",  # Lax is fine for same-domain navigation
                max_age=86400,  # 24 hours
                domain=host,    # Set on the custom domain
                path="/",
            )
            print(f"🍪 Custom domain session set for {host} → tunnel {tunnel_id}")
            return response

    # ============================================
    # Private custom domain auth gate
    # If the tunnel is private and the request has no valid session cookie,
    # redirect browser visitors to egdesk.cloud/auth/tunnel-login to authenticate.
    # ============================================
    if not is_public:
        session_cookie_name = f"egdesk_session_{tunnel_id}"
        session_token = request.cookies.get(session_cookie_name)
        has_valid_session = (
            session_token
            and session_token in iframe_sessions
            and iframe_sessions[session_token].get("tunnel_id") == tunnel_id
        )

        # Check session expiry
        if has_valid_session:
            expires_at = datetime.fromisoformat(iframe_sessions[session_token]["expires_at"])
            if datetime.utcnow() > expires_at:
                del iframe_sessions[session_token]
                has_valid_session = False

        if not has_valid_session:
            # Check for API key or Bearer token (non-browser clients)
            api_key_header = request.headers.get("X-Api-Key")
            auth_header = request.headers.get("Authorization")
            if api_key_header or auth_header:
                # Let tunnel_request handle API key / Bearer auth as normal
                pass
            else:
                # Browser visitor with no auth → redirect to egdesk.cloud login
                accept_header = request.headers.get("accept", "")
                if "text/html" in accept_header:
                    from urllib.parse import quote
                    raw_host = request.headers.get("host", host)
                    redirect_url = f"https://{raw_host}/{path}" if path else f"https://{raw_host}/"
                    login_url = f"https://egdesk.cloud/auth/tunnel-login?redirect={quote(redirect_url)}&tunnel_id={tunnel_id}"
                    print(f"🔒 Private custom domain {host}: redirecting browser to {login_url}")
                    from fastapi.responses import RedirectResponse
                    return RedirectResponse(url=login_url, status_code=302)

    display_path = path or ""
    print(f"🌐 Custom domain request: {host}/{display_path} → {tunnel_id}")
    # Pass the domain-resolved project through so the inner handler's own public/private
    # check agrees with the one just performed above, instead of re-deriving (and failing
    # to derive) the project name from the path a second time.
    return await _handle_tunnel_request(tunnel_id, path, request, project_override=request_project)


@app.middleware("http")
async def custom_domain_middleware(request: Request, call_next):
    """Route custom-domain HTTP traffic before explicit service endpoints can shadow it."""
    host = normalize_host(request.headers.get("host"))
    if host and host not in PRIMARY_DOMAINS:
        path = request.url.path.lstrip("/")
        custom_domain_response = await route_custom_domain_request(path, request)
        if custom_domain_response is not None:
            return custom_domain_response

    return await call_next(request)


@app.get("/")
async def root(request: Request):
    custom_domain_response = await route_custom_domain_request("", request)
    if custom_domain_response is not None:
        return custom_domain_response

    return {
        "service": "Tunnel Service",
        "active_tunnels": len(active_tunnels),
        "tunnel_ids": list(active_tunnels.keys()),
        "instructions": "Connect client via WebSocket to /tunnel/connect"
    }

@app.post("/custom-domains/register")
async def register_custom_domains(request: Request):
    """Register custom domains for a tunnel and ask Render to provision SSL."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    server_key = str(body.get("server_key") or "").strip()
    domains = normalize_custom_domains(body.get("domains") or [])

    if not server_key:
        return JSONResponse(status_code=400, content={"error": "server_key is required"})
    if not domains:
        return JSONResponse(status_code=400, content={"error": "At least one valid domain is required"})

    try:
        server_result = supabase.table("mcp_servers").select("*").or_(
            f"server_key.eq.{server_key},name.eq.{server_key}"
        ).execute()
        server_data = (server_result.data or [None])[0]
        if not server_data:
            return JSONResponse(status_code=404, content={"error": f"Server '{server_key}' not found"})

        is_owner, ownership_error = verify_ownership(request, server_data)
        if not is_owner:
            return JSONResponse(status_code=403, content={"error": ownership_error or "Forbidden"})

        updated_description = merge_custom_domains_description(server_data.get("description"), domains)
        supabase.table("mcp_servers").update({"description": updated_description}).eq("id", server_data["id"]).execute()

        for domain in domains:
            clear_domain_route_cache(domain)

        # Register all domains (bare + www) with Render for SSL certs
        render_results = []
        for domain in domains:
            result = await add_custom_domain_to_render(domain)
            render_results.append(result)

        return {
            "success": True,
            "server_key": server_data.get("server_key"),
            "domains": domains,
            "render_configured": all(item.get("success") for item in render_results),
            "render_results": render_results,
        }
    except Exception as e:
        print(f"❌ Failed to register custom domains: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/register")
async def register_mcp(request: Request):
    """Register MCP server - User ID-based ownership (with IP fallback for legacy)"""
    try:
        # Parse request body
        body = await request.json()
        name = body.get("name")
        server_key = body.get("server_key")
        description = body.get("description")
        connection_url = body.get("connection_url")
        max_concurrent_connections = body.get("max_concurrent_connections", 10)
        owner_email = body.get("owner_email")  # Optional: auto-add as invited user
        
        if not name or not server_key:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Missing required fields",
                    "message": "Both name and server_key are required"
                }
            )
        
        # Extract user from auth header (preferred ownership method)
        user_id, user_email = get_user_from_auth_header(request)
        
        # Get client IP (fallback for legacy, also for logging)
        client_ip = get_client_ip(request)
        
        # Use authenticated user's email if owner_email not provided
        if not owner_email and user_email:
            owner_email = user_email
        
        # Check if server_key is already taken
        existing = supabase.table("mcp_servers").select("id, name, server_key, owner_user_id, owner_ip, owner_ip_salt, created_at").eq("server_key", server_key).execute()
        
        if existing.data and len(existing.data) > 0:
            existing_record = existing.data[0]
            existing_owner_id = existing_record.get("owner_user_id")
            existing_ip_hash = existing_record.get("owner_ip")
            existing_salt = existing_record.get("owner_ip_salt")
            
            # Check ownership: User ID first, then IP fallback
            is_owner = False
            ownership_method = None
            
            # Method 1: User ID verification (preferred)
            if existing_owner_id and user_id:
                if user_id == existing_owner_id:
                    is_owner = True
                    ownership_method = "user_id"
                    print(f"✅ Re-registration ownership verified via User ID: {user_email}")
            
            # Method 2: IP verification (legacy fallback - only if no owner_user_id set)
            if not is_owner and not existing_owner_id:
                if verify_ip_ownership(client_ip, existing_ip_hash, existing_salt):
                    is_owner = True
                    ownership_method = "ip_legacy"
                    print(f"✅ Re-registration ownership verified via IP (legacy): {client_ip}")
            
            if is_owner:
                # Owner verified - update the record
                update_data = {
                    "name": name,
                    "description": description,
                    "connection_url": connection_url,
                    "max_concurrent_connections": max_concurrent_connections,
                    "updated_at": datetime.now().isoformat(),
                    "status": "active"
                }
                
                # Upgrade legacy IP-only servers to User ID ownership
                if ownership_method == "ip_legacy" and user_id:
                    update_data["owner_user_id"] = user_id
                    print(f"⬆️ Upgrading server '{server_key}' from IP to User ID ownership")
                
                update_result = supabase.table("mcp_servers").update(update_data).eq("server_key", server_key).execute()
                
                if update_result.data:
                    updated_data = update_result.data[0]
                    server_id = updated_data.get("id")
                    
                    # Auto-add owner permission if email was provided
                    owner_permission_added = False
                    if owner_email and server_id:
                        try:
                            existing_perm = supabase.table("mcp_server_permissions").select("id").eq("server_id", server_id).eq("allowed_email", owner_email.lower()).execute()
                            
                            if not existing_perm.data or len(existing_perm.data) == 0:
                                perm_result = supabase.table("mcp_server_permissions").insert({
                                    "server_id": server_id,
                                    "allowed_email": owner_email.lower(),
                                    "status": "active",
                                    "access_level": "admin",
                                    "granted_at": datetime.now().isoformat(),
                                    "activated_at": datetime.now().isoformat(),
                                    "notes": "Auto-granted: Server owner (re-registration)"
                                }).execute()
                                
                                if perm_result.data:
                                    owner_permission_added = True
                                    print(f"✅ Auto-added owner permission for {owner_email} (re-registration)")
                            else:
                                owner_permission_added = True
                                print(f"ℹ️ Owner permission already exists for {owner_email}")
                        except Exception as perm_error:
                            print(f"⚠️ Warning: Failed to handle owner permission on re-registration: {perm_error}")
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": f"MCP server '{name}' re-registered successfully",
                            "name": updated_data.get("name"),
                            "id": updated_data.get("id"),
                            "server_key": updated_data.get("server_key"),
                            "created_at": updated_data.get("created_at"),
                            "is_reregistration": True,
                            "owner_permission_added": owner_permission_added,
                            "ownership_upgraded": ownership_method == "ip_legacy" and user_id is not None
                        }
                    )
            else:
                # Not the owner - server key taken by someone else
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "error": "Server key already exists",
                        "message": f"Server key '{server_key}' is already registered by another user",
                        "existing_record": {
                            "name": existing_record.get("name"),
                            "server_key": existing_record.get("server_key"),
                            "registered_at": existing_record.get("created_at")
                        }
                    }
                )
        
        # Server key doesn't exist - create new registration
        # Enforce one MCP server per user (app-level check)
        if user_id:
            existing_user_servers = supabase.table("mcp_servers").select("id, name, server_key").eq("owner_user_id", user_id).execute()
            if existing_user_servers.data and len(existing_user_servers.data) > 0:
                existing_server = existing_user_servers.data[0]
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "error": "User already has a registered server",
                        "message": f"You already own server '{existing_server.get('name')}' (key: {existing_server.get('server_key')}). Each user can only have one MCP server.",
                        "existing_server": {
                            "name": existing_server.get("name"),
                            "server_key": existing_server.get("server_key"),
                            "id": existing_server.get("id")
                        }
                    }
                )

        result = supabase.table("mcp_servers").insert({
            "owner_user_id": user_id,
            "name": name,
            "description": description,
            "server_key": server_key,
            "connection_url": connection_url,
            "max_concurrent_connections": max_concurrent_connections,
            "status": "active"
        }).execute()
        
        if result.data and len(result.data) > 0:
            registered_data = result.data[0]
            server_id = registered_data.get("id")
            
            # Auto-add owner permission if email was provided
            owner_permission_added = False
            if owner_email and server_id:
                try:
                    perm_result = supabase.table("mcp_server_permissions").insert({
                        "server_id": server_id,
                        "allowed_email": owner_email.lower(),
                        "status": "active",
                        "access_level": "admin",
                        "granted_at": datetime.now().isoformat(),
                        "activated_at": datetime.now().isoformat(),
                        "notes": "Auto-granted: Server owner"
                    }).execute()
                    
                    if perm_result.data:
                        owner_permission_added = True
                        print(f"✅ Auto-added owner permission for {owner_email}")
                except Exception as perm_error:
                    print(f"⚠️ Warning: Failed to auto-add owner permission: {perm_error}")
            
            return JSONResponse(
                status_code=201,
                content={
                    "success": True,
                    "message": "MCP server registered successfully",
                    "name": registered_data.get("name"),
                    "id": registered_data.get("id"),
                    "server_key": registered_data.get("server_key"),
                    "created_at": registered_data.get("created_at"),
                    "is_reregistration": False,
                    "owner_permission_added": owner_permission_added
                }
            )
        else:
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "Database error",
                    "message": "Failed to register MCP server"
                }
            )
            
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Invalid JSON",
                "message": "Invalid JSON in request body"
            }
        )
    except Exception as e:
        print(f"❌ Registration error: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error",
                "message": str(e)
            }
        )


@app.post("/register/confirm-selection")
async def confirm_mcp_server_selection(request: Request):
    """Keep one MCP server for the authenticated user and delete all other owned servers."""
    try:
        user_id, user_email = get_user_from_auth_header(request)
        if not user_id:
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "error": "Unauthorized",
                    "message": "Authentication required",
                },
            )

        body = await request.json()
        server_key = (body.get("server_key") or "").strip().lower()
        if not server_key:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Missing required fields",
                    "message": "server_key is required",
                },
            )

        user_servers_result = (
            supabase.table("mcp_servers")
            .select("id, name, server_key, created_at")
            .eq("owner_user_id", user_id)
            .execute()
        )
        user_servers = user_servers_result.data or []

        keeper = next((s for s in user_servers if s.get("server_key") == server_key), None)
        if not keeper:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": "Server not found",
                    "message": f"No owned MCP server found with key '{server_key}'",
                },
            )

        servers_to_delete = [s for s in user_servers if s.get("id") != keeper.get("id")]
        deleted_keys: list[str] = []

        for server in servers_to_delete:
            server_id = server.get("id")
            server_key_to_delete = server.get("server_key")
            if not server_id:
                continue

            try:
                permissions = (
                    supabase.table("mcp_server_permissions")
                    .select("id")
                    .eq("server_id", server_id)
                    .execute()
                )
                for permission in permissions.data or []:
                    permission_id = permission.get("id")
                    if permission_id:
                        supabase.table("mcp_connection_sessions").delete().eq(
                            "permission_id", permission_id
                        ).execute()

                supabase.table("mcp_server_permissions").delete().eq(
                    "server_id", server_id
                ).execute()
                supabase.table("mcp_servers").delete().eq("id", server_id).execute()

                if server_key_to_delete:
                    deleted_keys.append(server_key_to_delete)
                    print(
                        f"🗑️ Deleted duplicate MCP server '{server_key_to_delete}' for user {user_email or user_id}"
                    )
            except Exception as delete_error:
                print(
                    f"❌ Failed to delete MCP server '{server_key_to_delete}' ({server_id}): {delete_error}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Delete failed",
                        "message": f"Failed to delete server '{server_key_to_delete}': {delete_error}",
                        "deleted_keys": deleted_keys,
                    },
                )

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": "MCP server selection confirmed",
                "server_key": keeper.get("server_key"),
                "server_name": keeper.get("name"),
                "server_id": keeper.get("id"),
                "deleted_count": len(deleted_keys),
                "deleted_keys": deleted_keys,
            },
        )
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Invalid JSON",
                "message": "Invalid JSON in request body",
            },
        )
    except Exception as e:
        print(f"❌ Confirm MCP server selection error: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error",
                "message": str(e),
            },
        )

@app.post("/permissions")
async def add_permissions(request: Request):
    """Add allowed email(s) to server - User ID or IP-based authentication"""
    try:
        # Parse request body
        body = await request.json()
        server_key = body.get("server_key")
        emails = body.get("emails", [])
        access_level = body.get("access_level", "read_write")
        expires_at = body.get("expires_at")
        notes = body.get("notes")
        
        if not server_key or not emails:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Missing required fields",
                    "message": "server_key and emails are required"
                }
            )
        
        # Verify server exists
        server = supabase.table("mcp_servers").select("*").eq("server_key", server_key).single().execute()
        
        if not server.data:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": "Server not found",
                    "message": f"Server '{server_key}' does not exist"
                }
            )
        
        # Verify ownership (User ID first, then IP fallback)
        is_owner, error_message = verify_ownership(request, server.data)
        
        if not is_owner:
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "error": "Permission denied",
                    "message": error_message
                }
            )
        
        # Get granter info for audit
        user_id, user_email = get_user_from_auth_header(request)
        
        # Add permissions for each email
        permissions_to_add = []
        for email in emails:
            permissions_to_add.append({
                "server_id": server.data["id"],
                "allowed_email": email.lower(),
                "status": "pending",
                "access_level": access_level,
                "granted_by_user_id": user_id,  # Track who granted (if authenticated)
                "expires_at": expires_at,
                "notes": notes
            })
        
        result = supabase.table("mcp_server_permissions").insert(permissions_to_add).execute()
        
        if result.data:
            return JSONResponse(
                status_code=201,
                content={
                    "success": True,
                    "message": f"Added {len(result.data)} permission(s)",
                    "added": len(result.data),
                    "permissions": result.data
                }
            )
        else:
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "Database error",
                    "message": "Failed to add permissions"
                }
            )
            
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Invalid JSON",
                "message": "Invalid JSON in request body"
            }
        )
    except Exception as e:
        print(f"❌ Add permissions error: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error",
                "message": str(e)
            }
        )

@app.get("/permissions/{server_key}")
async def get_permissions(server_key: str, request: Request):
    """Get all permissions for a server - User ID or IP-based authentication"""
    try:
        # Verify server exists
        server = supabase.table("mcp_servers").select("*").eq("server_key", server_key).single().execute()
        
        if not server.data:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": "Server not found",
                    "message": f"Server '{server_key}' does not exist"
                }
            )
        
        # Verify ownership (User ID first, then IP fallback)
        is_owner, error_message = verify_ownership(request, server.data)
        
        if not is_owner:
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "error": "Permission denied",
                    "message": error_message
                }
            )
        
        # Get all permissions for this server
        permissions = supabase.table("mcp_server_permissions").select("*").eq("server_id", server.data["id"]).execute()
        
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "server_key": server_key,
                "permissions": permissions.data or []
            }
        )
        
    except Exception as e:
        print(f"❌ Get permissions error: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error",
                "message": str(e)
            }
        )

@app.patch("/permissions/{permission_id}")
async def update_permission(permission_id: str, request: Request):
    """Update a permission - User ID or IP-based authentication"""
    try:
        # Get permission and associated server for ownership verification
        permission = supabase.table("mcp_server_permissions").select("*, mcp_servers!inner(owner_user_id, owner_ip, owner_ip_salt)").eq("id", permission_id).single().execute()
        
        if not permission.data:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": "Permission not found",
                    "message": f"Permission '{permission_id}' does not exist"
                }
            )
        
        # Verify ownership (User ID first, then IP fallback)
        server_data = permission.data["mcp_servers"]
        is_owner, error_message = verify_ownership(request, server_data)
        
        if not is_owner:
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "error": "Permission denied",
                    "message": error_message
                }
            )
        
        # Parse update fields
        body = await request.json()
        update_fields = {}
        
        if "access_level" in body:
            update_fields["access_level"] = body["access_level"]
        if "expires_at" in body:
            update_fields["expires_at"] = body["expires_at"]
        if "notes" in body:
            update_fields["notes"] = body["notes"]
        if "status" in body:
            update_fields["status"] = body["status"]
            if body["status"] == "revoked":
                update_fields["revoked_at"] = datetime.now().isoformat()
        
        if not update_fields:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "No update fields",
                    "message": "No fields to update"
                }
            )
        
        # Update permission
        result = supabase.table("mcp_server_permissions").update(update_fields).eq("id", permission_id).execute()
        
        if result.data:
            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": "Permission updated",
                    "permission": result.data[0]
                }
            )
        else:
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "Database error",
                    "message": "Failed to update permission"
                }
            )
            
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Invalid JSON",
                "message": "Invalid JSON in request body"
            }
        )
    except Exception as e:
        print(f"❌ Update permission error: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error",
                "message": str(e)
            }
        )

@app.delete("/permissions/{permission_id}")
async def delete_permission(permission_id: str, request: Request):
    """Revoke a permission - User ID or IP-based authentication"""
    try:
        # Get permission and associated server for ownership verification
        permission = supabase.table("mcp_server_permissions").select("*, mcp_servers!inner(owner_user_id, owner_ip, owner_ip_salt)").eq("id", permission_id).single().execute()
        
        if not permission.data:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": "Permission not found",
                    "message": f"Permission '{permission_id}' does not exist"
                }
            )
        
        # Verify ownership (User ID first, then IP fallback)
        server_data = permission.data["mcp_servers"]
        is_owner, error_message = verify_ownership(request, server_data)
        
        if not is_owner:
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "error": "Permission denied",
                    "message": error_message
                }
            )
        
        # Soft delete: mark as revoked instead of actually deleting
        result = supabase.table("mcp_server_permissions").update({
            "status": "revoked",
            "revoked_at": datetime.now().isoformat()
        }).eq("id", permission_id).execute()
        
        if result.data:
            # Also terminate any active sessions for this permission
            supabase.table("mcp_connection_sessions").update({
                "status": "terminated",
                "disconnected_at": datetime.now().isoformat()
            }).eq("permission_id", permission_id).eq("status", "active").execute()
            
            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": "Permission revoked and active sessions terminated"
                }
            )
        else:
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "Database error",
                    "message": "Failed to revoke permission"
                }
            )
            
    except Exception as e:
        print(f"❌ Delete permission error: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error",
                "message": str(e)
            }
        )

@app.websocket("/tunnel/connect")
async def tunnel_connect(websocket: WebSocket, name: str = None):
    """Client connects here to establish tunnel"""
    await websocket.accept()
    
    # Use server name as tunnel identifier
    if not name:
        await websocket.send_json({
            "type": "error",
            "message": "Server name is required as query parameter"
        })
        await websocket.close()
        return
    
    # Check if server is registered in mcp_servers table (check by server_key or name)
    # Try server_key first, then fallback to name for backwards compatibility
    existing = supabase.table("mcp_servers").select("id, name, server_key, status, owner_user_id, description").or_(f"server_key.eq.{name},name.eq.{name}").execute()
    
    if not existing.data or len(existing.data) == 0:
        await websocket.send_json({
            "type": "error",
            "message": f"Server '{name}' is not registered. Please register first."
        })
        await websocket.close()
        return
    
    server_data = existing.data[0]
    
    # Check if server is active
    if server_data.get("status") != "active":
        await websocket.send_json({
            "type": "error",
            "message": f"Server '{name}' is not active (status: {server_data.get('status')})"
        })
        await websocket.close()
        return
    
    # Note: We skip IP verification for WebSocket tunnel connections because:
    # 1. The server is registered via Supabase Edge Function which sees a different IP than Render.com
    # 2. Actual MCP requests through the tunnel are already protected by OAuth authentication
    # 3. The server owner is already authenticated via Supabase auth during registration
    # 
    # Security is maintained because:
    # - Only authenticated users can register servers (via Supabase Edge Function)
    # - All requests through the tunnel require valid OAuth tokens
    # - Permissions are checked for each request
    
    # Log connection info for debugging
    client_ip = None
    if websocket.scope.get("client"):
        client_ip = websocket.scope["client"][0]
    headers = dict(websocket.scope.get("headers", []))
    forwarded_for = headers.get(b"x-forwarded-for", b"").decode()
    real_ip = headers.get(b"x-real-ip", b"").decode()
    resolved_ip = real_ip or (forwarded_for.split(',')[0].strip() if forwarded_for else None) or client_ip or "unknown"
    
    print(f"🔌 Tunnel connection from IP: {resolved_ip} for server '{name}'")
    
    # Use the requested name (slug) as the tunnel_id for consistency with HTTP routes
    # even if the server_key in DB was accidentally overwritten with a UUID.
    tunnel_id = name
    
    # Check if tunnel with this ID already exists
    if tunnel_id in active_tunnels:
        # Close existing connection
        old_ws = active_tunnels[tunnel_id]
        try:
            await old_ws.close()
        except:
            pass
        print(f"⚠️  Replacing existing tunnel: {tunnel_id}")
    
    active_tunnels[tunnel_id] = websocket

    # Load per-project access flags from mcp_servers.description
    try:
        desc_raw = server_data.get("description", "{}")
        desc_json = json.loads(desc_raw) if desc_raw else {}
        flags = load_flags_from_description(desc_json)
        tunnel_public_flags[tunnel_id] = flags
        public_projects = [k for k, v in flags.items() if v and k != "__default__"]
        if flags.get("__default__"):
            print(f"🌍 Tunnel {tunnel_id} default is public")
        if public_projects:
            print(f"🌍 Tunnel {tunnel_id} public projects: {public_projects}")
        if not flags.get("__default__") and not public_projects:
            print(f"🔒 Tunnel {tunnel_id} is fully private — auth required")
    except Exception as e:
        print(f"⚠️ Could not read access flags for {tunnel_id}: {e}")
        tunnel_public_flags[tunnel_id] = {"__default__": False}

    print(f"✓ Tunnel established: {tunnel_id} (display name: {server_data.get('name')})")
    
    # Send tunnel info to client
    await websocket.send_json({
        "type": "connected",
        "tunnel_id": tunnel_id,
        "public_url": f"https://tunneling-service.onrender.com/t/{tunnel_id}"
    })
    
    # Heartbeat task to keep connection alive and detect disconnections
    async def heartbeat():
        """Send periodic pings to detect connection health"""
        try:
            while True:
                await asyncio.sleep(20)  # Ping every 20 seconds (reduced from 30s)
                try:
                    await websocket.send_json({"type": "ping", "timestamp": datetime.now().isoformat()})
                except Exception as e:
                    print(f"💔 Heartbeat failed for {tunnel_id}: {e}")
                    break
        except asyncio.CancelledError:
            pass
    
    # Start heartbeat task
    heartbeat_task = asyncio.create_task(heartbeat())
    
    try:
        # Listen for responses from client
        while True:
            try:
                data = await websocket.receive_json()
                print(f"📨 WebSocket message received: type={data.get('type')}, request_id={data.get('request_id')}")
            except json.JSONDecodeError as json_error:
                # JSON parsing error - log but continue
                print(f"⚠️ Invalid JSON received: {json_error}")
                continue
            except (WebSocketDisconnect, RuntimeError) as disconnect_error:
                # Connection closed - break out of loop
                if "disconnect" in str(disconnect_error).lower():
                    print(f"🔌 WebSocket disconnected: {tunnel_id}")
                else:
                    print(f"🔌 WebSocket error (connection closed): {tunnel_id} - {disconnect_error}")
                break
            except Exception as e:
                # Other errors - log and break to avoid infinite loop
                print(f"❌ Unexpected WebSocket error: {e}")
                break

            if data["type"] == "response":
                request_id = data["request_id"]
                print(f"📥 Received response for request {request_id}: status {data.get('status_code')}, body size: {len(data.get('body', ''))} bytes")
                if request_id in pending_requests:
                    # Resolve the pending request with response
                    print(f"✅ Resolving pending request {request_id}")
                    pending_requests[request_id].set_result(data)
                else:
                    print(f"⚠️  Received response for unknown request: {request_id}")

            elif data["type"] == "stream_chunk":
                # Handle streaming response chunks (for SSE)
                request_id = data["request_id"]
                if request_id in streaming_requests:
                    print(f"📦 Received stream chunk for {request_id}: {len(data.get('body', ''))} bytes")
                    await streaming_requests[request_id].put(data)
                else:
                    print(f"⚠️  Received stream chunk for unknown request: {request_id}")

            elif data["type"] == "stream_end":
                # End of streaming response
                request_id = data["request_id"]
                print(f"🏁 Received stream_end for {request_id}")
                if request_id in streaming_requests:
                    await streaming_requests[request_id].put(None)  # Signal end

            elif data.get("type") == "register_api_key":
                api_key = data.get("api_key")
                if api_key:
                    # Update Supabase instead of local memory
                    # Store the API key in the description JSON to avoid overwriting the server_key slug
                    try:
                        # Find server by server_key OR name (slug) to be resilient
                        server_data = supabase.table("mcp_servers").select("id, description").or_(f"server_key.eq.{tunnel_id},name.eq.{tunnel_id}").execute()
                        
                        if server_data.data:
                            existing_row = server_data.data[0]
                            desc_json = {}
                            try:
                                desc_json = json.loads(existing_row.get("description", "{}"))
                            except:
                                desc_json = {"raw_description": existing_row.get("description")}
                            
                            desc_json["api_key"] = api_key
                            supabase.table("mcp_servers").update({"description": json.dumps(desc_json)}).eq("id", existing_row["id"]).execute()
                            print(f"🔑 API key registered in Supabase for tunnel: {tunnel_id}")
                    except Exception as e:
                        print(f"❌ Failed to register API key in Supabase: {e}")

            elif data["type"] == "pong":
                # Client responded to ping - connection is healthy
                print(f"💓 Heartbeat acknowledged for {tunnel_id}")

            elif data["type"] == "ping":
                # Client sent a ping - respond with pong
                print(f"💓 Heartbeat received from {tunnel_id}, responding with pong")
                await websocket.send_json({"type": "pong", "timestamp": data.get("timestamp", datetime.now().isoformat())})

    except WebSocketDisconnect:
        print(f"✗ Tunnel disconnected: {tunnel_id}")
    except Exception as e:
        print(f"✗ Tunnel error for {tunnel_id}: {e}")
    finally:
        # Clean up
        heartbeat_task.cancel()
        if tunnel_id in active_tunnels:
            del active_tunnels[tunnel_id]
        if tunnel_id in tunnel_public_flags:
            del tunnel_public_flags[tunnel_id]

        # Clean up any pending streaming requests for this tunnel
        dead_streams = [req_id for req_id, queue in streaming_requests.items() if req_id.startswith(tunnel_id)]
        for req_id in dead_streams:
            streaming_requests[req_id].put_nowait(None)
            del streaming_requests[req_id]
        
        print(f"🧹 Cleaned up tunnel: {tunnel_id}")

@app.get("/t/{tunnel_id}/ping")
async def ping_server(tunnel_id: str, request: Request):
    """Health check endpoint - verifies if server is online and accessible"""
    
    print(f"🏓 Ping request for tunnel: {tunnel_id}")
    print(f"📊 Active tunnels: {list(active_tunnels.keys())}")
    
    # Extract Authorization header for authentication
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={
                "error": "Unauthorized",
                "message": "Missing or invalid Authorization header"
            }
        )
    
    # Verify token (basic check, not full permission validation for health check)
    access_token = auth_header.replace("Bearer ", "")
    try:
        user_response = supabase.auth.get_user(access_token)
        if not user_response or not user_response.user:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized",
                    "message": "Invalid or expired access token"
                }
            )
    except Exception as e:
        return JSONResponse(
            status_code=401,
            content={
                "error": "Authentication failed",
                "message": str(e)
            }
        )
    
    # Check if tunnel exists and is connected
    if tunnel_id not in active_tunnels:
        print(f"❌ Tunnel '{tunnel_id}' not found in active tunnels")
        return JSONResponse(
            status_code=503,
            content={
                "online": False,
                "error": "Server offline or not connected",
                "active_tunnels": list(active_tunnels.keys())
            }
        )
    
    # Server is online
    print(f"✅ Tunnel '{tunnel_id}' is online")
    from datetime import datetime as dt
    return JSONResponse(
        status_code=200,
        content={
            "online": True,
            "server_key": tunnel_id,
            "timestamp": dt.utcnow().isoformat()
        }
    )

@app.post("/t/{tunnel_id}/auth")
async def authenticate_session(tunnel_id: str, request: Request, response: Response):
    """
    Create an authenticated session for iframe access.
    Client calls this endpoint with Bearer token, receives a secure session cookie.
    """
    # Extract Authorization header
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={
                "error": "Unauthorized",
                "message": "Missing Authorization header"
            }
        )

    # Extract token
    access_token = auth_header.replace("Bearer ", "")

    try:
        # Verify token with Supabase Auth
        user_response = supabase.auth.get_user(access_token)

        if not user_response or not user_response.user:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized",
                    "message": "Invalid or expired access token"
                }
            )

        user = user_response.user
        user_email = user.email
        user_id = user.id

        print(f"🔐 Creating session for user: {user_email}")

        # Get server info by tunnel_id
        server = supabase.table("mcp_servers").select("*").eq("server_key", tunnel_id).single().execute()

        if not server.data:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "Server not found",
                    "message": f"Server '{tunnel_id}' does not exist"
                }
            )

        server_id = server.data["id"]

        # Owners always have access (same rule as session_exchange)
        is_owner = str(server.data.get("owner_user_id")) == str(user_id)
        if is_owner:
            permission_id = f"owner-{user_id}"
        else:
            # Check if user has permission to access this server
            permission = supabase.table("mcp_server_permissions").select("*").eq("server_id", server_id).eq("allowed_email", user_email).single().execute()

            if not permission.data:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "Forbidden",
                        "message": f"User '{user_email}' does not have permission to access this server."
                    }
                )

            # Check permission status
            perm_status = permission.data.get("status")
            if perm_status != "active" and perm_status != "pending":
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "Forbidden",
                        "message": "Your access to this server has been revoked or expired"
                    }
                )
            permission_id = permission.data["id"]

        # Create session token
        session_token = secrets.token_urlsafe(32)
        session_data = {
            "user_id": user_id,
            "user_email": user_email,
            "tunnel_id": tunnel_id,
            "server_id": server_id,
            "permission_id": permission_id,
            "created_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(hours=24)).isoformat()
        }

        # Store session
        iframe_sessions[session_token] = session_data

        print(f"✅ Session created for {user_email} on tunnel {tunnel_id}")

        # Create response with session cookie
        # For cross-origin iframes, we MUST use SameSite=None + Secure=True
        # This means the website MUST be served over HTTPS (works on Vercel, not on http://localhost)
        json_response = JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": "Session created successfully",
                "expires_in": 86400
            }
        )

        # Set secure HTTP-only cookie on the response we're returning
        json_response.set_cookie(
            key=f"egdesk_session_{tunnel_id}",
            value=session_token,
            httponly=True,
            secure=True,  # Required for SameSite=None (HTTPS only)
            samesite="none",  # Required for cross-origin iframes
            max_age=86400,  # 24 hours
            path=f"/t/{tunnel_id}/"  # Scoped to this tunnel
        )

        return json_response

    except Exception as e:
        print(f"❌ Authentication error: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "Authentication failed",
                "message": str(e)
            }
        )

@app.post("/t/{tunnel_id}/auth/session-exchange")
async def session_exchange(tunnel_id: str, request: Request):
    """
    Exchange a Supabase access token for a tunnel session token.
    Used by egdesk.cloud/auth/tunnel-login to create a session that can be
    passed back to a custom domain via redirect.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"error": "Missing Bearer token"})

    access_token = auth_header.replace("Bearer ", "")

    try:
        # Verify token with Supabase
        user_response = supabase.auth.get_user(access_token)
        if not user_response or not user_response.user:
            return JSONResponse(status_code=401, content={"error": "Invalid or expired token"})

        user = user_response.user
        user_email = user.email
        user_id = user.id

        # Get server info
        server = supabase.table("mcp_servers").select("*").or_(
            f"server_key.eq.{tunnel_id},name.eq.{tunnel_id}"
        ).execute()
        if not server.data:
            return JSONResponse(status_code=404, content={"error": "Server not found"})

        server_data = server.data[0]
        server_id = server_data["id"]

        # Check if user is the owner (owners always have access)
        is_owner = str(server_data.get("owner_user_id")) == str(user_id)

        if not is_owner:
            # Check permissions
            permission = supabase.table("mcp_server_permissions").select("*").eq(
                "server_id", server_id
            ).eq("allowed_email", user_email).execute()

            if not permission.data:
                return JSONResponse(status_code=403, content={
                    "error": "Forbidden",
                    "message": f"User '{user_email}' does not have permission to access this server."
                })

            perm_status = permission.data[0].get("status")
            if perm_status not in ("active", "pending"):
                return JSONResponse(status_code=403, content={
                    "error": "Forbidden",
                    "message": "Your access has been revoked or expired."
                })
            permission_id = permission.data[0]["id"]
        else:
            permission_id = f"owner-{user_id}"

        # Create session
        session_token = secrets.token_urlsafe(32)
        session_data = {
            "user_id": str(user_id),
            "user_email": user_email,
            "tunnel_id": tunnel_id,
            "server_id": server_id,
            "permission_id": permission_id,
            "created_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(hours=24)).isoformat(),
        }
        iframe_sessions[session_token] = session_data

        print(f"✅ Session exchange: {user_email} → tunnel {tunnel_id}")
        return JSONResponse(content={
            "success": True,
            "session_token": session_token,
            "expires_in": 86400,
        })

    except Exception as e:
        print(f"❌ Session exchange error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.put("/t/{tunnel_id}/access-mode")
async def set_tunnel_access_mode(tunnel_id: str, request: Request):
    """
    Set public/private access mode for a tunnel.
    Only the tunnel owner (verified via Supabase Bearer token) can change this.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    public_access = bool(body.get("public", False))
    project_name = body.get("project")  # Optional: per-project access mode

    # Verify the requester is the tunnel owner
    server_check = supabase.table("mcp_servers").select("*").or_(
        f"server_key.eq.{tunnel_id},name.eq.{tunnel_id}"
    ).execute()

    if not server_check.data:
        return JSONResponse(status_code=404, content={"error": "Tunnel not found"})

    server_data = server_check.data[0]
    is_owner, err = verify_ownership(request, server_data)
    if not is_owner:
        return JSONResponse(status_code=403, content={"error": err or "Forbidden"})

    # Update description JSON in Supabase
    try:
        desc_json = {}
        try:
            desc_json = json.loads(server_data.get("description", "{}") or "{}")
        except Exception:
            pass

        if project_name:
            # Per-project access mode
            project_access = desc_json.get("project_access") or {}
            project_access[project_name] = public_access
            desc_json["project_access"] = project_access
        else:
            # Tunnel-wide default
            desc_json["public_access"] = public_access

        supabase.table("mcp_servers").update({
            "description": json.dumps(desc_json)
        }).eq("id", server_data["id"]).execute()
    except Exception as e:
        print(f"❌ Failed to update access mode in Supabase: {e}")
        return JSONResponse(status_code=500, content={"error": "Failed to persist access mode"})

    # Update in-memory flags (effective immediately for connected tunnels)
    if not isinstance(tunnel_public_flags.get(tunnel_id), dict):
        tunnel_public_flags[tunnel_id] = {"__default__": False}
    if project_name:
        tunnel_public_flags[tunnel_id][project_name] = public_access
    else:
        tunnel_public_flags[tunnel_id]["__default__"] = public_access

    target = f"project '{project_name}'" if project_name else "tunnel default"
    mode = "public" if public_access else "private"
    print(f"🔧 Tunnel {tunnel_id} {target} access mode set to {mode}")

    return JSONResponse(content={
        "success": True, "tunnel_id": tunnel_id, "public": public_access,
        **({"project": project_name} if project_name else {}),
    })


@app.put("/t/{tunnel_id}/domain-mapping")
async def set_tunnel_domain_mapping(tunnel_id: str, request: Request):
    """
    Sync domain -> project mappings for a tunnel.
    Used by the Electron app to ensure the remote router knows which project
    a custom domain belongs to for access control.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    domain = body.get("domain")
    project_name = body.get("project") # Can be None to clear mapping

    if not domain:
        return JSONResponse(status_code=400, content={"error": "domain is required"})

    # Verify ownership
    server_check = supabase.table("mcp_servers").select("*").or_(
        f"server_key.eq.{tunnel_id},name.eq.{tunnel_id}"
    ).execute()

    if not server_check.data:
        return JSONResponse(status_code=404, content={"error": "Tunnel not found"})

    server_data = server_check.data[0]
    is_owner, err = verify_ownership(request, server_data)
    if not is_owner:
        return JSONResponse(status_code=403, content={"error": err or "Forbidden"})

    try:
        desc_json = parse_description_json(server_data.get("description"))
        domain_project_map = desc_json.get("domain_project_map") or {}
        
        # We update both the bare domain and the www variant
        variants = [domain.lower()]
        if domain.startswith("www."):
            variants.append(domain[4:].lower())
        else:
            variants.append(f"www.{domain}".lower())

        for v in variants:
            if project_name:
                domain_project_map[v] = project_name
            else:
                domain_project_map.pop(v, None)

        desc_json["domain_project_map"] = domain_project_map
        desc_json["domain_project_map_updated_at"] = datetime.utcnow().isoformat()

        supabase.table("mcp_servers").update({
            "description": json.dumps(desc_json)
        }).eq("id", server_data["id"]).execute()
        
        # Clear cache so next request sees the new mapping
        for v in variants:
            clear_domain_route_cache(v)

        print(f"🌐 Updated domain mapping for {tunnel_id}: {domain} -> {project_name}")
        return {"success": True, "domain": domain, "project": project_name}
    except Exception as e:
        print(f"❌ Failed to update domain mapping in Supabase: {e}")
        return JSONResponse(status_code=500, content={"error": "Failed to persist domain mapping"})


@app.api_route("/t/{tunnel_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def tunnel_request(tunnel_id: str, path: str, request: Request):
    """Public endpoint - forwards requests through tunnel with OAuth authentication.

    Thin wrapper with no extra params, so nothing here is attacker-controllable via
    query string. Direct /t/{tunnel_id}/... links have no custom-domain context, so
    project resolution falls back to path-extraction inside _handle_tunnel_request.
    """
    return await _handle_tunnel_request(tunnel_id, path, request)


async def _handle_tunnel_request(
    tunnel_id: str,
    path: str,
    request: Request,
    project_override: str | None = None,
):
    """
    Shared implementation for tunnel_request and custom-domain routing.

    project_override lets route_custom_domain_request supply the project name it
    already resolved from domain_project_map, since custom-domain paths never carry
    a "/p/{project}/" prefix and extract_project_from_path() alone can't find it.
    This parameter must never be reachable from the public /t/{tunnel_id}/{path}
    route directly (see tunnel_request above) — only from trusted internal callers.
    """
    
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization, X-Api-Key, X-EGDesk-Project-Id, X-EGDesk-Env",
            }
        )

    # ============================================
    # Session token from redirect callback
    # When egdesk.cloud redirects back with ?__egdesk_session=<token>,
    # validate the session, set a cookie on this tunnel path, and redirect to clean URL.
    # ============================================
    session_param = request.query_params.get("__egdesk_session")
    if session_param and session_param in iframe_sessions:
        session_data = iframe_sessions[session_param]
        if session_data.get("tunnel_id") == tunnel_id:
            from urllib.parse import urlencode
            from fastapi.responses import RedirectResponse
            raw_host = request.headers.get("host", "tunneling-service.onrender.com")
            clean_path = f"/t/{tunnel_id}/{path}" if path else f"/t/{tunnel_id}/"
            other_params = {k: v for k, v in request.query_params.items() if k != "__egdesk_session"}
            qs = f"?{urlencode(other_params)}" if other_params else ""
            clean_url = f"https://{raw_host}{clean_path}{qs}"

            response = RedirectResponse(url=clean_url, status_code=302)
            response.set_cookie(
                key=f"egdesk_session_{tunnel_id}",
                value=session_param,
                httponly=True,
                secure=True,
                samesite="lax",
                max_age=86400,
                path=f"/t/{tunnel_id}",
            )
            print(f"🍪 Tunnel path session set for /t/{tunnel_id}")
            return response

    # Check if tunnel exists locally
    if tunnel_id not in active_tunnels:
        print(f"❌ Tunnel '{tunnel_id}' not found locally. Active local tunnels: {list(active_tunnels.keys())}")
        return JSONResponse(
            status_code=404,
            content={"error": "Tunnel not found or disconnected"}
        )

    # ============================================
    # Public access check — per-project or tunnel-wide
    # ============================================
    request_project = project_override or extract_project_from_path(path)
    is_public_tunnel = is_project_public(tunnel_id, request_project)

    # ============================================
    # Public pass-through paths (no auth required)
    # ============================================
    PUBLIC_PATHS = {"kakao/skill", "webhook/start"}
    if is_public_tunnel:
        target = f"project '{request_project}'" if request_project else "tunnel"
        print(f"🌍 Public {target}: {tunnel_id} — skipping auth")
        session_token = None
    elif path not in PUBLIC_PATHS:
        # ============================================
        # Session Cookie Authentication (for iframes)
        # ============================================
        session_cookie_name = f"egdesk_session_{tunnel_id}"
        session_token = request.cookies.get(session_cookie_name)

        # Debug logging
        print(f"🍪 Looking for cookie: {session_cookie_name}")
        print(f"🍪 All cookies received: {list(request.cookies.keys())}")
        print(f"🍪 Session token found: {bool(session_token)}")
    else:
        print(f"🔓 Public path bypass: /{path} — skipping tunnel auth")
        session_token = None

    if not is_public_tunnel and path not in PUBLIC_PATHS and session_token and session_token in iframe_sessions:
        session_data = iframe_sessions[session_token]

        # Check if session expired
        expires_at = datetime.fromisoformat(session_data["expires_at"])
        if datetime.utcnow() > expires_at:
            # Session expired - remove it
            del iframe_sessions[session_token]
            return JSONResponse(
                status_code=401,
                content={"error": "Session expired", "message": "Please re-authenticate"}
            )

        # Verify session is for this tunnel
        if session_data["tunnel_id"] != tunnel_id:
            return JSONResponse(
                status_code=403,
                content={"error": "Invalid session", "message": "Session not valid for this tunnel"}
            )

        print(f"🍪 Session auth: {session_data['user_email']} → {tunnel_id}")
        # Session is valid - skip OAuth check and proceed to forwarding
        # We'll set user_id and user_email for session tracking later
        user_id = session_data["user_id"]
        user_email = session_data["user_email"]
        server_id = session_data["server_id"]
        permission_id = session_data["permission_id"]

        # Owner sessions use a synthetic permission_id (see session_exchange) — skip DB tracking.
        track_connection_session(
            server_id=server_id,
            user_id=user_id,
            permission_id=permission_id,
            session_token=session_token,
        )

        # Skip to request forwarding (jump past OAuth/API key checks)
        pass  # Continue to request forwarding below

    # ============================================
    # API Key Authentication (Apps Script / service accounts)
    # ============================================
    elif not is_public_tunnel and path not in PUBLIC_PATHS and (api_key_header := request.headers.get("X-Api-Key")):
        # Query Supabase for the key stored in the description JSON
        try:
            # Find server by server_key OR name (slug) to be resilient
            server_check = supabase.table("mcp_servers").select("description").or_(f"server_key.eq.{tunnel_id},name.eq.{tunnel_id}").execute()
            stored_key = None
            if server_check.data:
                try:
                    desc_json = json.loads(server_check.data[0].get("description", "{}"))
                    stored_key = desc_json.get("api_key")
                except:
                    pass
            
            if not stored_key or stored_key != api_key_header:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Unauthorized", "message": "Invalid API key"}
                )
            print(f"🔑 API key auth granted via Supabase for tunnel: {tunnel_id}")
        except Exception as e:
            print(f"⚠️ API key check failed: {e}")
            return JSONResponse(status_code=500, content={"error": "Authentication check failed"})
    elif not is_public_tunnel and path not in PUBLIC_PATHS:
        # ============================================
        # OAuth Authentication & Authorization
        # ============================================

        # Extract token from Authorization header OR query parameter
        # Query parameter support is for iframe-based access where headers can't be set
        auth_header = request.headers.get("Authorization")
        access_token = None

        if auth_header and auth_header.startswith("Bearer "):
            # Token from header (preferred method)
            access_token = auth_header.replace("Bearer ", "")
        else:
            # Token from query parameter (for iframe embedding)
            access_token = request.query_params.get("token")

        if not access_token:
            # Redirect browser visitors to egdesk.cloud tunnel-login page
            accept_header = request.headers.get("accept", "")
            if "text/html" in accept_header:
                from fastapi.responses import RedirectResponse
                from urllib.parse import quote
                redirect_url = str(request.url)
                login_url = f"https://egdesk.cloud/auth/tunnel-login?redirect={quote(redirect_url)}&tunnel_id={tunnel_id}"
                return RedirectResponse(url=login_url, status_code=302)
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized",
                    "message": "Missing Authorization header, X-Api-Key, or token query parameter. Use Supabase OAuth or provide X-Api-Key."
                }
            )

        try:
            # Verify token with Supabase Auth
            user_response = supabase.auth.get_user(access_token)

            if not user_response or not user_response.user:
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "Unauthorized",
                        "message": "Invalid or expired access token"
                    }
                )

            user = user_response.user
            user_email = user.email
            user_id = user.id

            print(f"🔐 Authenticated user: {user_email}")

            # Get server info by tunnel_id (which is server_key)
            server = supabase.table("mcp_servers").select("*").eq("server_key", tunnel_id).single().execute()

            if not server.data:
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": "Server not found",
                        "message": f"Server '{tunnel_id}' does not exist"
                    }
                )

            server_id = server.data["id"]

            # Owners always have access without an explicit permission row
            is_owner = str(server.data.get("owner_user_id")) == str(user_id)
            if is_owner:
                print(f"✅ Owner OAuth access granted for {user_email} to access {tunnel_id}")
            else:
                # Check if user has permission to access this server
                permission = supabase.table("mcp_server_permissions").select("*").eq("server_id", server_id).eq("allowed_email", user_email).single().execute()

                if not permission.data:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "Forbidden",
                            "message": f"User '{user_email}' does not have permission to access this server. Contact the server owner to request access."
                        }
                    )

                # Check permission status
                perm_status = permission.data.get("status")
                if perm_status == "revoked":
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "Forbidden",
                            "message": "Your access to this server has been revoked"
                        }
                    )
                elif perm_status == "expired":
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "Forbidden",
                            "message": "Your access to this server has expired"
                        }
                    )

                # Check expiration date
                expires_at = permission.data.get("expires_at")
                if expires_at:
                    expiry = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                    if datetime.now(expiry.tzinfo) > expiry:
                        # Auto-expire the permission
                        supabase.table("mcp_server_permissions").update({"status": "expired"}).eq("id", permission.data["id"]).execute()
                        return JSONResponse(
                            status_code=403,
                            content={
                                "error": "Forbidden",
                                "message": "Your access to this server has expired"
                            }
                        )

                # If permission is pending, activate it on first use
                if perm_status == "pending":
                    supabase.table("mcp_server_permissions").update({
                        "status": "active",
                        "activated_at": datetime.utcnow().isoformat(),
                        "user_id": user_id
                    }).eq("id", permission.data["id"]).execute()
                    print(f"✅ Activated permission for {user_email}")

                track_connection_session(
                    server_id=server_id,
                    user_id=user_id,
                    permission_id=permission.data["id"],
                )

            print(f"✅ Authorization granted for {user_email} to access {tunnel_id}")

        except Exception as e:
            print(f"❌ Authentication error: {e}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Authentication failed",
                    "message": str(e)
                }
            )
    
    # ============================================
    # Forward Request to Tunnel
    # ============================================

    websocket = active_tunnels[tunnel_id]
    request_id = str(uuid.uuid4())

    # Read request body
    body = await request.body()

    # Check if this is a coding project route (/p/{project_name}/...)
    full_path = "/" + path
    is_project_route = full_path.startswith("/p/")
    if is_project_route:
        # Extract project name for logging
        path_parts = full_path.split("/")
        if len(path_parts) >= 3:
            project_name = path_parts[2]
            project_path = "/" + "/".join(path_parts[3:]) if len(path_parts) > 3 else "/"
            print(f"🔀 Routing to coding project: {project_name} → {project_path}")

    # Prepare request data to send to client
    # Inject x-forwarded-host with the public tunnel domain so that frameworks
    # like Next.js don't reject the request due to host/origin mismatch.
    # The tunnel client's httpx will override the `host` header to localhost,
    # but x-forwarded-host takes precedence for origin validation checks.
    forwarded_headers = dict(request.headers)
    forwarded_headers["x-forwarded-host"] = request.headers.get("host", "tunneling-service.onrender.com")

    request_data = {
        "type": "request",
        "request_id": request_id,
        "method": request.method,
        "path": full_path,
        "headers": forwarded_headers,
        "query_params": dict(request.query_params),
        "body": body.decode() if body else None,
        "tunnel_id": tunnel_id  # Include tunnel_id for base path construction
    }
    
    # Check if this is an SSE request (GET to /sse endpoint)
    is_sse = request.method == "GET" and ("/sse" in path or path.endswith("/sse"))
    
    if is_sse:
        print(f"🔵 Detected SSE request: {request.method} {path}")
        # Handle SSE streaming request
        stream_queue = asyncio.Queue()
        streaming_requests[request_id] = stream_queue
        
        # Track if client disconnected
        client_disconnected = asyncio.Event()
        
        async def stream_generator():
            try:
                # Send request to client
                await websocket.send_json(request_data)
                print(f"📡 SSE stream started: {request_id}")
                
                # Stream responses as they come (indefinitely until client disconnects)
                while not client_disconnected.is_set():
                    try:
                        # Wait for chunks (with timeout for keepalive)
                        chunk_data = await asyncio.wait_for(stream_queue.get(), timeout=30.0)
                        
                        if chunk_data is None:
                            # End of stream signal from client
                            print(f"📡 SSE stream ended by server: {request_id}")
                            break
                        
                        # Yield the chunk body
                        if "body" in chunk_data:
                            yield chunk_data["body"]
                    
                    except asyncio.TimeoutError:
                        # Send keepalive comment to prevent connection timeout
                        yield ": keepalive\n\n"
                        
            except asyncio.CancelledError:
                # Client disconnected - this is raised when the HTTP connection closes
                print(f"🔌 SSE client disconnected: {request_id}")
                client_disconnected.set()
                
                # Notify tunnel client to stop streaming
                try:
                    await websocket.send_json({
                        "type": "stream_cancel",
                        "request_id": request_id
                    })
                except:
                    pass
                    
            except Exception as e:
                print(f"❌ Stream error for {request_id}: {e}")
            finally:
                # Clean up
                if request_id in streaming_requests:
                    del streaming_requests[request_id]
                print(f"🧹 Cleaned up stream: {request_id}")
        
        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*"
            }
        )
    
    else:
        # Handle regular request/response
        future = asyncio.Future()
        pending_requests[request_id] = future

        try:
            # Send request to client through WebSocket
            print(f"📤 Sending request {request_id} to client via WebSocket")
            await websocket.send_json(request_data)
            # Wait for response — no timeout, let requests take as long as needed.
            # The client-side browser/fetch timeout will handle hung requests.
            print(f"⏳ Waiting for response to request {request_id} (no timeout)")
            response_data = await future

            print(f"✅ Received response for {request_id}, status: {response_data.get('status_code')}")

            # Clean up
            del pending_requests[request_id]

            # Get headers and remove Content-Length (let FastAPI recalculate it)
            # This is important because the tunnel client may have injected content (like <base> tags)
            # which changes the body length
            response_headers = response_data.get("headers", {})
            if "content-length" in response_headers:
                del response_headers["content-length"]
            if "Content-Length" in response_headers:
                del response_headers["Content-Length"]

            # Return response to original requester
            response = Response(
                content=response_data.get("body", ""),
                status_code=response_data.get("status_code", 200),
                headers=response_headers
            )

            # Set a cookie so subsequent requests (even without Referer) can find this tunnel.
            # This handles dev mode where client-side navigation drops the /t/{id}/p/{name} prefix.
            request_project = project_override or extract_project_from_path(path)
            if request_project:
                response.set_cookie(
                    key="__egdesk_tunnel_ctx",
                    value=f"{tunnel_id}:{request_project}",
                    httponly=True,
                    secure=True,
                    samesite="lax",
                    max_age=86400,
                    path="/",
                )

            return response

        except Exception as e:
            if request_id in pending_requests:
                del pending_requests[request_id]
            return JSONResponse(
                status_code=500,
                content={"error": str(e)}
            )

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def custom_domain_request(path: str, request: Request):
    """Catch-all for custom domains. Reads Host and routes to the owning tunnel."""
    custom_domain_response = await route_custom_domain_request(path, request)
    if custom_domain_response is not None:
        return custom_domain_response

    # ============================================
    # Referer-based tunnel routing fallback
    # Next.js basePath doesn't apply to fetch() calls, so client-side code
    # may call /api/... without the /t/{id}/p/{name} prefix. Use the Referer
    # header to detect which tunnel the request belongs to and proxy it there.
    # ============================================
    host = normalize_host(request.headers.get("host"))
    if host in PRIMARY_DOMAINS:
        import re
        tunnel_id = None
        project_name = None

        # Method 1: Extract tunnel context from Referer header
        referer = request.headers.get("referer", "")
        match = re.search(r'/t/([^/]+)/p/([^/]+)', referer)
        if match:
            tunnel_id = match.group(1)
            project_name = match.group(2)

        # Method 2: Fall back to cookie (survives client-side navigation that drops the prefix)
        if not tunnel_id:
            ctx_cookie = request.cookies.get("__egdesk_tunnel_ctx")
            if ctx_cookie and ":" in ctx_cookie:
                parts = ctx_cookie.split(":", 1)
                tunnel_id = parts[0]
                project_name = parts[1]

        if tunnel_id and project_name:
            corrected_path = f"p/{project_name}/{path}"
            print(f"🔀 Auto-routing: /{path} → /t/{tunnel_id}/{corrected_path}")
            return await tunnel_request(tunnel_id, corrected_path, request)

    # Log why we couldn't route this request
    referer = request.headers.get("referer", "")
    print(f"❌ 404 catch-all: /{path} | host={host} | referer={referer[:120] if referer else '(none)'} | method={request.method}")
    return JSONResponse(status_code=404, content={"error": "Not found"})

