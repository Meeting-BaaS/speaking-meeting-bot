import asyncio
import logging
import os
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
    count: int = 1  # Default to 1, effectively making this a "per-bot" request
    meeting_url: str
    personas: Optional[List[str]] = None
    recorder_only: bool = False
    websocket_url: Optional[str] = None
    meeting_baas_api_key: str


@app.get("/")
async def root():
    return {"message": "MeetingBaas Bot API is running"}


@app.post("/run-bots")
async def run_bots(request: BotRequest):
    """
    Create a single bot with its own WebSocket server.
    For multiple bots, clients should make multiple API calls.
    """
    # Require a websocket_url or return an error
    if not request.websocket_url:
        return {"message": "WebSocket URL is required", "status": "error"}, 400

    # Create a BotProxyManager instance and run it
    manager = BotProxyManager()
    asyncio.create_task(
        manager.async_main(
            count=request.count,
            meeting_url=request.meeting_url,
            websocket_url=request.websocket_url,  # No default fallback
            personas=request.personas,
            recorder_only=request.recorder_only,
            meeting_baas_api_key=request.meeting_baas_api_key,
        )
    )

    return {
        "message": f"Starting bot for meeting {request.meeting_url}",
        "status": "success",
        "websocket_url": request.websocket_url,
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
