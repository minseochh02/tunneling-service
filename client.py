import asyncio
import websockets
import httpx
import json
import sys

async def tunnel_client(server_url: str, local_port: int):
    """Connect to tunnel server and forward requests to local server"""
    
    uri = f"{server_url.replace('https://', 'wss://').replace('http://', 'ws://')}/tunnel/connect"
    
    print(f"Connecting to tunnel server: {server_url}")
    
    async with websockets.connect(uri) as websocket:
        # Wait for connection confirmation
        init_msg = await websocket.recv()
        init_data = json.loads(init_msg)
        
        if init_data["type"] == "connected":
            tunnel_id = init_data["tunnel_id"]
            public_url = init_data["public_url"]
            
            print(f"\n✓ Tunnel established!")
            print(f"Public URL: {public_url}")
            print(f"Forwarding to: http://localhost:{local_port}")
            print(f"\nPress Ctrl+C to disconnect\n")
        
        # Track active streaming requests
        active_streams = {}
        
        # Listen for incoming requests
        async for message in websocket:
            data = json.loads(message)
            
            if data["type"] == "request":
                # Forward request to local server
                task = asyncio.create_task(
                    handle_request(websocket, data, local_port, active_streams)
                )
                # Track streaming requests
                if data.get("path", "").endswith("/sse") or "/sse" in data.get("path", ""):
                    active_streams[data["request_id"]] = task
            
            elif data["type"] == "ping":
                # Respond to heartbeat ping
                await websocket.send(json.dumps({
                    "type": "pong",
                    "timestamp": data.get("timestamp")
                }))
            
            elif data["type"] == "stream_cancel":
                # Client disconnected from SSE stream - cancel the streaming task
                request_id = data.get("request_id")
                if request_id in active_streams:
                    print(f"🛑 Cancelling stream: {request_id}")
                    active_streams[request_id].cancel()
                    del active_streams[request_id]

async def handle_request(websocket, request_data, local_port, active_streams):
    """Forward request to local server and send response back"""
    
    request_id = request_data["request_id"]
    method = request_data["method"]
    path = request_data["path"]
    headers = request_data["headers"]
    body = request_data.get("body")
    
    print(f"→ {method} {path}")
    
    # Check if this is a streaming SSE request
    is_sse = method == "GET" and ("/sse" in path or path.endswith("/sse"))
    
    try:
        # Make request to local server
        local_url = f"http://localhost:{local_port}{path}"
        
        async with httpx.AsyncClient() as client:
            if is_sse:
                # Handle SSE streaming
                print(f"🔵 Starting SSE stream: {request_id}")
                async with client.stream(
                    method=method,
                    url=local_url,
                    headers=headers,
                    content=body.encode() if body else None,
                    timeout=None  # No timeout for streaming
                ) as response:
                    try:
                        async for chunk in response.aiter_text():
                            # Send chunk to tunnel server
                            await websocket.send(json.dumps({
                                "type": "stream_chunk",
                                "request_id": request_id,
                                "body": chunk
                            }))
                        
                        # Stream ended normally
                        await websocket.send(json.dumps({
                            "type": "stream_end",
                            "request_id": request_id
                        }))
                        print(f"✓ SSE stream completed: {request_id}")
                        
                    except asyncio.CancelledError:
                        # Stream was cancelled (client disconnected)
                        print(f"🛑 SSE stream cancelled: {request_id}")
                        await websocket.send(json.dumps({
                            "type": "stream_end",
                            "request_id": request_id
                        }))
                        raise
                        
            else:
                # Handle regular request/response
                response = await client.request(
                    method=method,
                    url=local_url,
                    headers=headers,
                    content=body.encode() if body else None,
                    timeout=30.0
                )
                
                # Send response back through tunnel
                response_data = {
                    "type": "response",
                    "request_id": request_id,
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": response.text
                }
                
                await websocket.send(json.dumps(response_data))
                print(f"← {response.status_code} {method} {path}")
        
    except asyncio.CancelledError:
        # Task was cancelled - this is normal for stream cancellation
        print(f"🛑 Request cancelled: {request_id}")
        if request_id in active_streams:
            del active_streams[request_id]
        raise
        
    except Exception as e:
        print(f"✗ Error: {e}")
        # Send error response
        error_response = {
            "type": "response",
            "request_id": request_id,
            "status_code": 502,
            "headers": {},
            "body": json.dumps({"error": str(e)})
        }
        await websocket.send(json.dumps(error_response))
    
    finally:
        # Clean up from active_streams if it was tracked
        if request_id in active_streams:
            del active_streams[request_id]

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python client.py <server_url> <local_port>")
        print("Example: python client.py https://your-app.onrender.com 3000")
        sys.exit(1)
    
    server_url = sys.argv[1]
    local_port = int(sys.argv[2])
    
    try:
        asyncio.run(tunnel_client(server_url, local_port))
    except KeyboardInterrupt:
        print("\n\nTunnel disconnected")

