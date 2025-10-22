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
    """Register MCP client with name and IP address"""
    try:
        # Parse request body
        body = await request.json()
        name = body.get("name")
        password = body.get("password")  # Optional password field
        
        if not name:
            return JSONResponse(
                status_code=400,
                content={"error": "Name parameter is required"}
            )
        
        # Extract IP address
        # Try X-Forwarded-For header first (for proxied requests)
        forwarded_for = request.headers.get("x-forwarded-for")
        # Get the client's direct IP
        client_host = request.client.host if request.client else None
        
        # Extract user agent
        user_agent = request.headers.get("user-agent")
        
        # Check if name already exists
        existing = supabase.table("mcp").select("*").eq("name", name).execute()
        
        if existing.data and len(existing.data) > 0:
            existing_record = existing.data[0]
            existing_ip = existing_record.get("ip")
            
            # Check if it's the same IP (owner trying to re-register)
            if existing_ip == client_host:
                # Same IP - update the record and allow re-registration
                update_result = supabase.table("mcp").update({
                    "updated_at": datetime.now().isoformat(),
                    "forwarded_for": forwarded_for,
                    "user_agent": user_agent,
                    "password": password
                }).eq("name", name).execute()
                
                if update_result.data:
                    updated_data = update_result.data[0]
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": f"MCP server '{name}' re-registered successfully (same owner)",
                            "name": updated_data.get("name"),
                            "id": updated_data.get("id"),
                            "ip": updated_data.get("ip"),
                            "created_at": updated_data.get("created_at"),
                            "updated_at": updated_data.get("updated_at"),
                            "is_reregistration": True
                        }
                    )
            else:
                # Different IP - name is taken by someone else
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "error": f"MCP server with name '{name}' already exists",
                        "message": "Cannot register. This name is already taken by a different IP address. Please choose a different name.",
                        "existing_ip": existing_ip,
                        "your_ip": client_host
                    }
                )
        
        # Name doesn't exist - create new registration
        result = supabase.table("mcp").insert({
            "name": name,
            "ip": client_host,
            "forwarded_for": forwarded_for,
            "user_agent": user_agent,
            "password": password
        }).execute()
        
        if result.data:
            registered_data = result.data[0]
            return JSONResponse(
                status_code=201,
                content={
                    "success": True,
                    "message": f"MCP server '{name}' registered successfully",
                    "name": registered_data.get("name"),
                    "id": registered_data.get("id"),
                    "ip": registered_data.get("ip"),
                    "created_at": registered_data.get("created_at"),
                    "is_reregistration": False
                }
            )
        else:
            return JSONResponse(
                status_code=500,
                content={"error": "Failed to register MCP client"}
            )
            
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON in request body"}
        )
    except Exception as e:
        print(f"❌ Registration error: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Registration failed: {str(e)}"}
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
    
    # Check if name is registered in database
    existing = supabase.table("mcp").select("name").eq("name", name).execute()
    if not existing.data or len(existing.data) == 0:
        await websocket.send_json({
            "type": "error",
            "message": f"Server name '{name}' is not registered"
        })
        await websocket.close()
        return
    
    # Check if tunnel with this name already exists
    if name in active_tunnels:
        # Close existing connection
        old_ws = active_tunnels[name]
        try:
            await old_ws.close()
        except:
            pass
        print(f"⚠️  Replacing existing tunnel: {name}")
    
    tunnel_id = name
    active_tunnels[tunnel_id] = websocket
    
    print(f"✓ Tunnel established: {tunnel_id}")
    
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

@app.api_route("/t/{tunnel_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def tunnel_request(tunnel_id: str, path: str, request: Request):
    """Public endpoint - forwards requests through tunnel"""
    
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