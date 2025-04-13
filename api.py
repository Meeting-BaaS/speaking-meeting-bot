import json
import logging
import os
import uuid
from typing import Dict, List, Optional

import uvicorn
import websockets
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl

import protobufs.frames_pb2 as frames_pb2  # Import Protobuf definitions
from scripts.meetingbaas_api import create_meeting_bot

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
    bot_image: Optional[str] = None
    entry_message: Optional[str] = None
    extra: Optional[Dict] = None


@app.get("/")
async def root():
    return {"message": "MeetingBaas Bot API is running"}


@app.post("/run-bots")
async def run_bots(request: BotRequest):
    """
    Create a bot directly via MeetingBaas API and establish WebSocket connection.
    """
    # Validate required parameters
    if not request.websocket_url:
        return JSONResponse(
            content={"message": "WebSocket URL is required", "status": "error"},
            status_code=400,
        )

    if not request.meeting_url:
        return JSONResponse(
            content={"message": "Meeting URL is required", "status": "error"},
            status_code=400,
        )

    if not request.meeting_baas_api_key:
        return JSONResponse(
            content={"message": "MeetingBaas API key is required", "status": "error"},
            status_code=400,
        )

    logger.info(f"Starting bot for meeting {request.meeting_url}")
    logger.info(f"WebSocket URL: {request.websocket_url}")
    logger.info(f"Personas: {request.personas}")

    # Generate a UUID for the bot
    bot_client_id = str(uuid.uuid4())[:8]

    # Select the persona (use first one if provided, otherwise "default")
    persona_name = (
        request.personas[0]
        if request.personas and len(request.personas) > 0
        else "default"
    )

    # Create bot directly through MeetingBaas API
    meetingbaas_bot_id = create_meeting_bot(
        meeting_url=request.meeting_url,
        websocket_url=request.websocket_url,
        bot_id=bot_client_id,
        persona_name=persona_name,
        api_key=request.meeting_baas_api_key,
        recorder_only=request.recorder_only,
        bot_image=request.bot_image,
        entry_message=request.entry_message,
        extra=request.extra,
    )

    if meetingbaas_bot_id:
        return {
            "message": f"Bot successfully created for meeting {request.meeting_url}",
            "status": "success",
            "websocket_url": request.websocket_url,
            "bot_id": meetingbaas_bot_id,
            "client_id": bot_client_id,
        }
    else:
        return JSONResponse(
            content={
                "message": "Failed to create bot through MeetingBaas API",
                "status": "error",
            },
            status_code=500,
        )


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """Handle WebSocket connections from clients (MeetingBaas)"""
    logger.info(f"Received WebSocket connection attempt from client {client_id}")
    try:
        await manager.connect(websocket, client_id)
        logger.info(f"WebSocket connection established with client {client_id}")

        # Track if we've already logged the first audio chunk
        first_audio_logged = False

        while True:
            message = await websocket.receive()
            if "bytes" in message:
                data = message["bytes"]
                # Only log the first audio data received
                if not first_audio_logged:
                    logger.info(
                        f"Receiving audio data from client {client_id} ({len(data)} bytes per chunk)"
                    )
                    first_audio_logged = True
                # Forward binary data to Pipecat with conversion
                await manager.send_to_pipecat(data, client_id)
            elif "text" in message:
                data = message["text"]
                # Try to parse as JSON and pretty print speakers
                try:
                    json_data = json.loads(data)
                    # Check if this is speaker data (contains name and isSpeaking fields)
                    if (
                        isinstance(json_data, list)
                        and len(json_data) > 0
                        and "name" in json_data[0]
                        and "isSpeaking" in json_data[0]
                    ):
                        for speaker in json_data:
                            status = (
                                "started speaking"
                                if speaker.get("isSpeaking")
                                else "stopped speaking"
                            )
                            logger.info(
                                f"👤 {speaker.get('name')} ({speaker.get('id')}) {status}"
                            )
                    else:
                        # Regular JSON message
                        logger.info(
                            f"JSON message from client {client_id}:\n{json.dumps(json_data, indent=2)}"
                        )
                except json.JSONDecodeError:
                    # Not JSON, just a regular text message
                    logger.info(
                        f"Received text message from client {client_id}: {data}"
                    )

                # Handle text messages (could be control commands)
                await manager.broadcast(f"Client {client_id} says: {data}")
    except WebSocketDisconnect:
        logger.warning(f"WebSocket disconnected for client {client_id}")
        manager.disconnect(client_id)
    except Exception as e:
        logger.error(f"Error in WebSocket handler: {str(e)}")
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
