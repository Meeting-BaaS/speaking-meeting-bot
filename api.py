import asyncio
import json
import logging
import os
from typing import Dict, List, Optional

import uvicorn
import websockets
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

import protobufs.frames_pb2 as frames_pb2  # Import Protobuf definitions
from scripts.batch import BotProxyManager

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("meetingbaas-api")

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
        self.pipecat_connections: Dict[str, WebSocket] = {}
        self.logger = logger
        self.sample_rate = 24000  # Default sample rate for audio
        self.channels = 1  # Default number of channels

    async def connect(
        self, websocket: WebSocket, client_id: str, is_pipecat: bool = False
    ):
        await websocket.accept()
        if is_pipecat:
            self.pipecat_connections[client_id] = websocket
            self.logger.info(f"Pipecat client {client_id} connected")
        else:
            self.active_connections[client_id] = websocket
            self.logger.info(f"Client {client_id} connected")

    def disconnect(self, client_id: str, is_pipecat: bool = False):
        if is_pipecat and client_id in self.pipecat_connections:
            del self.pipecat_connections[client_id]
            self.logger.info(f"Pipecat client {client_id} disconnected")
        elif client_id in self.active_connections:
            del self.active_connections[client_id]
            self.logger.info(f"Client {client_id} disconnected")

    async def send_binary(self, message: bytes, client_id: str):
        """Send binary data to a client"""
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_bytes(message)
            self.logger.debug(f"Sent {len(message)} bytes to client {client_id}")

    async def send_to_pipecat(self, message: bytes, client_id: str):
        """Convert raw audio to Protobuf frame and send to Pipecat"""
        if client_id in self.pipecat_connections:
            try:
                # Create Protobuf frame for the audio data
                frame = frames_pb2.Frame()
                frame.audio.audio = message
                frame.audio.sample_rate = self.sample_rate
                frame.audio.num_channels = self.channels

                # Serialize and send the frame
                serialized_frame = frame.SerializeToString()
                await self.pipecat_connections[client_id].send_bytes(serialized_frame)
                self.logger.debug(
                    f"Forwarded audio frame ({len(message)} bytes) to Pipecat for client {client_id}"
                )
            except Exception as e:
                self.logger.error(f"Error sending to Pipecat: {str(e)}")

    async def send_from_pipecat(self, message: bytes, client_id: str):
        """Extract audio from Protobuf frame and send to client"""
        if client_id in self.active_connections:
            try:
                frame = frames_pb2.Frame()
                frame.ParseFromString(message)
                if frame.HasField("audio"):
                    audio_data = frame.audio.audio
                    audio_size = len(audio_data)
                    await self.active_connections[client_id].send_bytes(
                        bytes(audio_data)
                    )
                    self.logger.debug(
                        f"Forwarded audio ({audio_size} bytes) from Pipecat to client {client_id}"
                    )
            except Exception as e:
                self.logger.error(f"Error processing Pipecat message: {str(e)}")

    async def send_text(self, message: str, client_id: str):
        """Send text message to a specific client"""
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_text(message)
            self.logger.debug(
                f"Sent text message to client {client_id}: {message[:100]}..."
            )

    async def broadcast(self, message: str):
        """Broadcast text message to all clients"""
        for client_id, connection in self.active_connections.items():
            await connection.send_text(message)
            self.logger.debug(f"Broadcast text message to client {client_id}")


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

    logger.info(f"Starting bot for meeting {request.meeting_url}")
    logger.info(f"WebSocket URL: {request.websocket_url}")
    logger.info(f"Personas: {request.personas}")

    # Create a BotProxyManager instance and run it
    bot_manager = BotProxyManager()
    asyncio.create_task(
        bot_manager.async_main(
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
    """Handle WebSocket connections from clients (MeetingBaas)"""
    await manager.connect(websocket, client_id)
    try:
        while True:
            # Handle both binary and text messages
            message = await websocket.receive()
            if "bytes" in message:
                data = message["bytes"]
                logger.debug(
                    f"Received binary data ({len(data)} bytes) from client {client_id}"
                )
                # Forward binary data to Pipecat with conversion
                await manager.send_to_pipecat(data, client_id)
            elif "text" in message:
                data = message["text"]
                # Log the text message
                logger.info(
                    f"Received text message from client {client_id}: {data[:100]}..."
                )

                # Try to parse as JSON
                try:
                    json_data = json.loads(data)
                    logger.info(
                        f"JSON message received from client {client_id}: {json.dumps(json_data, indent=2)}"
                    )
                except json.JSONDecodeError:
                    # Not JSON, just a regular text message
                    pass

                # Handle text messages (could be control commands)
                await manager.broadcast(f"Client {client_id} says: {data}")
    except WebSocketDisconnect:
        manager.disconnect(client_id)
    except Exception as e:
        logger.error(f"Error in WebSocket handler for client {client_id}: {str(e)}")
        manager.disconnect(client_id)


@app.websocket("/pipecat/{client_id}")
async def pipecat_websocket(websocket: WebSocket, client_id: str):
    """Handle WebSocket connections from Pipecat"""
    await manager.connect(websocket, client_id, is_pipecat=True)
    try:
        while True:
            message = await websocket.receive()
            if "bytes" in message:
                data = message["bytes"]
                logger.debug(
                    f"Received binary data ({len(data)} bytes) from Pipecat client {client_id}"
                )
                # Forward Pipecat messages to client with conversion
                await manager.send_from_pipecat(data, client_id)
            elif "text" in message:
                data = message["text"]
                logger.info(
                    f"Received text message from Pipecat client {client_id}: {data[:100]}..."
                )
    except WebSocketDisconnect:
        manager.disconnect(client_id, is_pipecat=True)
    except Exception as e:
        logger.error(
            f"Error in Pipecat WebSocket handler for client {client_id}: {str(e)}"
        )
        manager.disconnect(client_id, is_pipecat=True)


def start_server(host: str = "0.0.0.0", port: int = 8000):
    """Start the WebSocket server"""
    logger.info(f"Starting WebSocket server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start_server()
