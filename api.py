import os
import sys
import asyncio
import logging
import uvicorn
from typing import Dict, Optional, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from scripts.batch import BotProxyManager

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.logger = logging.getLogger(__name__)

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        self.logger.info(f"Client {client_id} connected")

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            self.logger.info(f"Client {client_id} disconnected")

    async def send_message(self, message: str, client_id: str):
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_text(message)

    async def broadcast(self, message: str):
        for connection in self.active_connections.values():
            await connection.send_text(message)

manager = ConnectionManager()

class BotRequest(BaseModel):
    count: int
    meeting_url: str
    personas: list[str] | None = None
    recorder_only: bool = False
    websocket_url: str | None = None

@app.get("/")
async def root():
    return {"message": "MeetingBaas Bot API is running"}

@app.post("/run-bots")
async def run_bots(request: BotRequest):
    # Start WebSocket server if no URL provided
    websocket_url = request.websocket_url or "ws://localhost:8000"
    
    # Update sys.argv for bot manager
    sys.argv = [
        "batch.py",
        "-c", str(request.count),
        "--meeting-url", request.meeting_url,
        "--websocket-url", websocket_url,
    ]
    
    if request.personas:
        sys.argv += ["--personas"] + request.personas
        
    if request.recorder_only:
        sys.argv.append("--recorder-only")
    
    # Run the bot logic in background
    manager = BotProxyManager()
    asyncio.create_task(manager.async_main())
    
    return {
        "message": f"Starting {request.count} bots for meeting {request.meeting_url}",
        "status": "success",
        "websocket_url": websocket_url
    }

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await manager.connect(websocket, client_id)
    try:
        while True:
            data = await websocket.receive_text()
            # Handle incoming messages here
            await manager.broadcast(f"Client {client_id} says: {data}")
    except WebSocketDisconnect:
        manager.disconnect(client_id)

def start_server(host: str = "0.0.0.0", port: int = 8000):
    """Start the WebSocket server"""
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    start_server()