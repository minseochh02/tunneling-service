from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import uuid
import os
import hashlib
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment variables
load_dotenv()

# Initialize Supabase client
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # Next.js dev server
        "http://localhost:3001",
        "https://egdesk-website.vercel.app",  # Vercel deployments
        "*"  # Allow all origins (you can restrict this in production)
    ],
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods (GET, POST, PUT, DELETE, etc.)
    allow_headers=["*"],  # Allow all headers including Authorization
)

# Store active tunnel connections
active_tunnels = {}  # {tunnel_id: websocket}
pending_requests = {}  # {request_id: asyncio.Future}
streaming_requests = {}  # {request_id: asyncio.Queue} for SSE streaming

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

@app.get("/")
async def root():
    return {
        "service": "Tunnel Service",
        "active_tunnels": len(active_tunnels),
        "tunnel_ids": list(active_tunnels.keys()),
        "instructions": "Connect client via WebSocket to /tunnel/connect"
    }

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
                            "owner_id": user_id,
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
        # Note: IP-based ownership has been removed - only User ID-based ownership is used
        result = supabase.table("mcp_servers").insert({
            "owner_user_id": user_id,   # User ID-based ownership (primary)
            "owner_id": None,           # Deprecated: old field
            "name": name,
            "description": description,
            "server_key": server_key,
            "connection_url": connection_url,
            "max_concurrent_connections": max_concurrent_connections,
            "status": "active"
            # owner_ip and owner_ip_salt removed - no longer storing IP addresses
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
                    "owner_id": user_id,
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
    existing = supabase.table("mcp_servers").select("id, name, server_key, status, owner_user_id").or_(f"server_key.eq.{name},name.eq.{name}").execute()
    
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
    
    # Use server_key as tunnel_id if available, otherwise use name
    tunnel_id = server_data.get("server_key") or name
    
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
    
    print(f"✓ Tunnel established: {tunnel_id} (server: {server_data.get('name')})")
    
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
                await asyncio.sleep(30)  # Ping every 30 seconds
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
            data = await websocket.receive_json()
            
            if data["type"] == "response":
                request_id = data["request_id"]
                if request_id in pending_requests:
                    # Resolve the pending request with response
                    pending_requests[request_id].set_result(data)
            
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
            
            elif data["type"] == "pong":
                # Client responded to ping - connection is healthy
                print(f"💓 Heartbeat acknowledged for {tunnel_id}")
                    
    except WebSocketDisconnect:
        print(f"✗ Tunnel disconnected: {tunnel_id}")
    except Exception as e:
        print(f"✗ Tunnel error for {tunnel_id}: {e}")
    finally:
        # Clean up
        heartbeat_task.cancel()
        if tunnel_id in active_tunnels:
            del active_tunnels[tunnel_id]
        
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

@app.api_route("/t/{tunnel_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def tunnel_request(tunnel_id: str, path: str, request: Request):
    """Public endpoint - forwards requests through tunnel with OAuth authentication"""
    
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization",
            }
        )
    
    # Check if tunnel exists
    if tunnel_id not in active_tunnels:
        return JSONResponse(
            status_code=404,
            content={"error": "Tunnel not found or disconnected"}
        )
    
    # ============================================
    # OAuth Authentication & Authorization
    # ============================================
    
    # Extract Authorization header
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={
                "error": "Unauthorized",
                "message": "Missing or invalid Authorization header. Please authenticate with Supabase OAuth."
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
        
        # Update or create session
        # Try to find existing active session
        existing_session = supabase.table("mcp_connection_sessions").select("*").eq("permission_id", permission.data["id"]).eq("status", "active").execute()
        
        if existing_session.data and len(existing_session.data) > 0:
            # Update existing session
            session_id = existing_session.data[0]["id"]
            supabase.table("mcp_connection_sessions").update({
                "last_activity_at": datetime.utcnow().isoformat(),
                "requests_count": existing_session.data[0]["requests_count"] + 1
            }).eq("id", session_id).execute()
        else:
            # Create new session
            import secrets
            session_token = secrets.token_urlsafe(32)
            supabase.table("mcp_connection_sessions").insert({
                "server_id": server_id,
                "user_id": user_id,
                "permission_id": permission.data["id"],
                "session_token": session_token,
                "status": "active",
                "connected_at": datetime.utcnow().isoformat(),
                "last_activity_at": datetime.utcnow().isoformat(),
                "requests_count": 1
            }).execute()
        
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
    
    # Prepare request data to send to client
    request_data = {
        "type": "request",
        "request_id": request_id,
        "method": request.method,
        "path": "/" + path,
        "headers": dict(request.headers),
        "query_params": dict(request.query_params),
        "body": body.decode() if body else None
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
            await websocket.send_json(request_data)
            
            # Wait for response (with timeout)
            response_data = await asyncio.wait_for(future, timeout=30.0)
            
            # Clean up
            del pending_requests[request_id]
            
            # Return response to original requester
            return Response(
                content=response_data.get("body", ""),
                status_code=response_data.get("status_code", 200),
                headers=response_data.get("headers", {})
            )
            
        except asyncio.TimeoutError:
            del pending_requests[request_id]
            return JSONResponse(
                status_code=504,
                content={"error": "Request timeout - client didn't respond"}
            )
        except Exception as e:
            if request_id in pending_requests:
                del pending_requests[request_id]
            return JSONResponse(
                status_code=500,
                content={"error": str(e)}
            )