from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import JSONResponse
import asyncio
import json
import uuid
from datetime import datetime

app = FastAPI()

# Store active tunnel connections
active_tunnels = {}  # {tunnel_id: websocket}
pending_requests = {}  # {request_id: asyncio.Future}

@app.get("/")
async def root():
    return {
        "service": "Tunnel Service",
        "active_tunnels": len(active_tunnels),
        "instructions": "Connect client via WebSocket to /tunnel/connect"
    }

@app.websocket("/tunnel/connect")
async def tunnel_connect(websocket: WebSocket):
    """Client connects here to establish tunnel"""
    await websocket.accept()
    
    # Generate unique tunnel ID
    tunnel_id = str(uuid.uuid4())[:8]
    active_tunnels[tunnel_id] = websocket
    
    print(f"✓ Tunnel established: {tunnel_id}")
    
    # Send tunnel info to client
    await websocket.send_json({
        "type": "connected",
        "tunnel_id": tunnel_id,
        "public_url": f"https://your-app.onrender.com/t/{tunnel_id}"
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
    
    # Create a future to wait for response
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