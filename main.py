from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
import asyncio
import json
import uuid
import os
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

# Store active tunnel connections
active_tunnels = {}  # {tunnel_id: websocket}
pending_requests = {}  # {request_id: asyncio.Future}
streaming_requests = {}  # {request_id: asyncio.Queue} for SSE streaming

@app.get("/")
async def root():
    return {
        "service": "Tunnel Service",
        "active_tunnels": len(active_tunnels),
        "instructions": "Connect client via WebSocket to /tunnel/connect"
    }

@app.post("/register")
async def register_mcp(request: Request):
    """Register MCP server - IP-based, no authentication required"""
    try:
        # Parse request body
        body = await request.json()
        name = body.get("name")
        server_key = body.get("server_key")
        description = body.get("description")
        connection_url = body.get("connection_url")
        max_concurrent_connections = body.get("max_concurrent_connections", 10)
        
        if not name or not server_key:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Missing required fields",
                    "message": "Both name and server_key are required"
                }
            )
        
        # Extract IP information
        forwarded_for = request.headers.get("x-forwarded-for")
        real_ip = request.headers.get("x-real-ip")
        client_host = request.client.host if request.client else None
        
        # Get client IP (prioritize real-ip, then forwarded-for, then client host)
        client_ip = real_ip or (forwarded_for.split(',')[0].strip() if forwarded_for else None) or client_host or "unknown"
        
        # Check if server_key is already taken
        existing = supabase.table("mcp_servers").select("id, name, server_key, owner_ip, created_at").eq("server_key", server_key).execute()
        
        if existing.data and len(existing.data) > 0:
            existing_record = existing.data[0]
            existing_ip = existing_record.get("owner_ip")
            
            # Check if it's the same IP (owner trying to re-register/update)
            if existing_ip == client_ip:
                # Same IP - update the record
                update_result = supabase.table("mcp_servers").update({
                    "name": name,
                    "description": description,
                    "connection_url": connection_url,
                    "max_concurrent_connections": max_concurrent_connections,
                    "updated_at": datetime.now().isoformat(),
                    "status": "active"
                }).eq("server_key", server_key).execute()
                
                if update_result.data:
                    updated_data = update_result.data[0]
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": f"MCP server '{name}' re-registered successfully",
                            "name": updated_data.get("name"),
                            "id": updated_data.get("id"),
                            "server_key": updated_data.get("server_key"),
                            "ip": client_ip,
                            "created_at": updated_data.get("created_at"),
                            "is_reregistration": True
                        }
                    )
            else:
                # Different IP - server key taken by someone else
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "error": "Server key already exists",
                        "message": f"Server key '{server_key}' is already registered by a different IP address",
                        "existing_record": {
                            "name": existing_record.get("name"),
                            "server_key": existing_record.get("server_key"),
                            "registered_at": existing_record.get("created_at")
                        }
                    }
                )
        
        # Server key doesn't exist - create new registration
        result = supabase.table("mcp_servers").insert({
            "owner_ip": client_ip,
            "owner_id": None,  # Optional: can be linked later via OAuth
            "name": name,
            "description": description,
            "server_key": server_key,
            "connection_url": connection_url,
            "max_concurrent_connections": max_concurrent_connections,
            "status": "active"
        }).execute()
        
        if result.data and len(result.data) > 0:
            registered_data = result.data[0]
            return JSONResponse(
                status_code=201,
                content={
                    "success": True,
                    "message": "MCP server registered successfully",
                    "name": registered_data.get("name"),
                    "id": registered_data.get("id"),
                    "server_key": registered_data.get("server_key"),
                    "ip": client_ip,
                    "created_at": registered_data.get("created_at"),
                    "is_reregistration": False
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
    existing = supabase.table("mcp_servers").select("id, name, server_key, status").or_(f"server_key.eq.{name},name.eq.{name}").execute()
    
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
                    
    except WebSocketDisconnect:
        print(f"✗ Tunnel disconnected: {tunnel_id}")
        del active_tunnels[tunnel_id]

@app.api_route("/t/{tunnel_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def tunnel_request(tunnel_id: str, path: str, request: Request):
    """Public endpoint - forwards requests through tunnel"""
    
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
    
    if tunnel_id not in active_tunnels:
        return JSONResponse(
            status_code=404,
            content={"error": "Tunnel not found or disconnected"}
        )
    
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
        
        async def stream_generator():
            try:
                # Send request to client
                await websocket.send_json(request_data)
                print(f"📡 SSE stream started: {request_id}")
                
                # Stream responses as they come (indefinitely until client disconnects)
                while True:
                    try:
                        # Wait for chunks (with timeout for keepalive)
                        chunk_data = await asyncio.wait_for(stream_queue.get(), timeout=30.0)
                        
                        if chunk_data is None:
                            # End of stream signal from client
                            print(f"📡 SSE stream ended by client: {request_id}")
                            break
                        
                        # Yield the chunk body
                        if "body" in chunk_data:
                            yield chunk_data["body"]
                    
                    except asyncio.TimeoutError:
                        # Send keepalive comment to prevent connection timeout
                        yield ": keepalive\n\n"
                        
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