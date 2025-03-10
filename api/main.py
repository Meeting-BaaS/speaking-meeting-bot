"""
Speaking Meeting Bot API

This module provides a REST API for the Speaking Meeting Bot, allowing users to
create and manage meeting bots with configurable personas.
"""
import os
import asyncio
import subprocess
import traceback
import threading
from typing import Dict, List, Optional, Any, Union

from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from loguru import logger

# Import existing functionality from the project
from config.persona_utils import PersonaManager
from config.prompts import DEFAULT_SYSTEM_PROMPT, DEFAULT_ENTRY_MESSAGE
import scripts.meetingbaas as meetingbaas
from meetingbaas_pipecat.utils.logger import configure_logger

# Configure logger
logger = configure_logger()

# Initialize FastAPI
app = FastAPI(
    title="Speaking Meeting Bot API",
    description="API for creating and managing AI meeting participants with custom personas",
    version="1.0.0",
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize persona manager
persona_manager = PersonaManager()

# Dictionary to track running bots
active_bots = {}


# Models for API requests and responses
class PersonaBase(BaseModel):
    """Base model for persona data"""
    name: str
    prompt: Optional[str] = DEFAULT_SYSTEM_PROMPT
    entry_message: Optional[str] = DEFAULT_ENTRY_MESSAGE
    characteristics: Optional[List[str]] = []
    tone_of_voice: Optional[List[str]] = []
    skin_tone: Optional[str] = None
    gender: Optional[str] = None
    relevant_links: Optional[List[str]] = []
    image: Optional[str] = ""
    cartesia_voice_id: Optional[str] = None
    language: Optional[str] = "en-US"


class PersonaCreate(PersonaBase):
    """Model for creating a new persona"""
    key: str = Field(..., description="Unique identifier for the persona (snake_case)")

    @validator('key')
    def key_must_be_snake_case(cls, v):
        if ' ' in v:
            raise ValueError('key must be in snake_case format')
        return v.lower()


class PersonaResponse(PersonaBase):
    """Model for persona response"""
    key: str


class PersonaUpdate(PersonaBase):
    """Model for updating an existing persona"""
    pass


class BotCreate(BaseModel):
    """Model for creating a new bot"""
    meeting_url: str = Field(..., description="URL of the meeting to join")
    persona_key: Optional[str] = Field(None, description="Key of the persona to use. If not provided, a random persona will be selected")
    ngrok_url: Optional[str] = Field(None, description="Optional WebSocket URL for the bot. If not provided, a new ngrok tunnel will be created.")
    recorder_only: bool = Field(False, description="Whether the bot is a recorder only")
    count: int = Field(1, description="Number of bots to create with the same configuration (max 5)")
    
    @validator('count')
    def validate_count(cls, v):
        if not isinstance(v, int):
            raise ValueError('Count must be an integer')
        if v < 1 or v > 5:
            raise ValueError('Count must be between 1 and 5')
        return v


class BotResponse(BaseModel):
    """Model for bot response"""
    bot_id: str
    meeting_url: str
    persona_key: str
    status: str
    recorder_only: bool


class ImageGenerationRequest(BaseModel):
    """Model for image generation request"""
    persona_key: str = Field(..., description="Key of the persona to generate an image for")
    replicate_key: Optional[str] = None
    utfs_key: Optional[str] = None
    app_id: Optional[str] = None


class APIResponse(BaseModel):
    """Standard API response model"""
    success: bool
    message: str
    data: Optional[Any] = None


# Helper function to get bot status
def get_bot_status(bot_id: str) -> str:
    """Get the status of a bot"""
    if bot_id not in active_bots:
        return "not_found"
        
    bot_info = active_bots[bot_id]
    
    # For recorder-only bots, we can't check the process status
    if bot_info["recorder_only"]:
        return "running"  # Assume it's running
    
    # For normal bots, check both bot and proxy processes
    bot_running = False
    proxy_running = False
    
    if "bot_process" in bot_info and bot_info["bot_process"]:
        bot_running = bot_info["bot_process"].poll() is None
        
    if "proxy_process" in bot_info and bot_info["proxy_process"]:
        proxy_running = bot_info["proxy_process"].poll() is None
    
    # Return status based on both processes
    if bot_running and proxy_running:
        return "running"
    elif bot_running or proxy_running:
        return "partial"  # One of the processes is running
    else:
        return "stopped"


# Endpoints
@app.get("/", response_model=APIResponse)
async def root():
    """Root endpoint - provides API information"""
    return {
        "success": True,
        "message": "Speaking Meeting Bot API is running",
        "data": {
            "version": "1.0.0",
            "documentation": "/docs"
        }
    }


# Persona Endpoints
@app.get("/personas", response_model=APIResponse)
async def list_personas():
    """List all available personas"""
    try:
        personas = []
        for key in persona_manager.list_personas():
            persona = persona_manager.get_persona(key)
            personas.append({**persona, "key": key})
        
        return {
            "success": True,
            "message": f"Retrieved {len(personas)} personas",
            "data": personas
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error retrieving personas: {str(e)}",
            "data": None
        }


@app.get("/personas/{key}", response_model=APIResponse)
async def get_persona(key: str):
    """Get details for a specific persona"""
    try:
        persona = persona_manager.get_persona(key)
        return {
            "success": True,
            "message": f"Retrieved persona: {key}",
            "data": {**persona, "key": key}
        }
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Persona with key {key} not found"
        )
    except Exception as e:
        return {
            "success": False,
            "message": f"Error retrieving persona: {str(e)}",
            "data": None
        }


@app.post("/personas", response_model=APIResponse)
async def create_persona(persona: PersonaCreate):
    """Create a new persona"""
    try:
        # Check if persona already exists
        if persona.key in persona_manager.personas:
            return {
                "success": False,
                "message": f"Persona with key {persona.key} already exists",
                "data": None
            }
        
        # Create persona dictionary from model
        persona_data = persona.dict(exclude={"key"})
        
        # Save the persona
        success = persona_manager.save_persona(persona.key, persona_data)
        
        if not success:
            return {
                "success": False,
                "message": "Failed to save persona",
                "data": None
            }
        
        return {
            "success": True,
            "message": f"Created persona: {persona.key}",
            "data": {**persona_data, "key": persona.key}
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error creating persona: {str(e)}",
            "data": None
        }


@app.put("/personas/{key}", response_model=APIResponse)
async def update_persona(key: str, persona: PersonaUpdate):
    """Update an existing persona"""
    try:
        # Check if persona exists
        if key not in persona_manager.personas:
            raise HTTPException(
                status_code=404,
                detail=f"Persona with key {key} not found"
            )
        
        # Create persona dictionary from model
        persona_data = persona.dict(exclude_unset=True)
        
        # Get existing persona data and update with new data
        existing_persona = persona_manager.get_persona(key)
        updated_persona = {**existing_persona, **persona_data}
        
        # Save the updated persona
        success = persona_manager.save_persona(key, updated_persona)
        
        if not success:
            return {
                "success": False,
                "message": "Failed to update persona",
                "data": None
            }
        
        return {
            "success": True,
            "message": f"Updated persona: {key}",
            "data": {**updated_persona, "key": key}
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        return {
            "success": False,
            "message": f"Error updating persona: {str(e)}",
            "data": None
        }


@app.delete("/personas/{key}", response_model=APIResponse)
async def delete_persona(key: str):
    """Delete a persona"""
    try:
        # Check if persona exists
        if key not in persona_manager.personas:
            raise HTTPException(
                status_code=404,
                detail=f"Persona with key {key} not found"
            )
        
        # Remove the persona
        success = persona_manager.delete_persona(key)
        
        if not success:
            return {
                "success": False,
                "message": "Failed to delete persona",
                "data": None
            }
        
        return {
            "success": True,
            "message": f"Deleted persona: {key}",
            "data": None
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        return {
            "success": False,
            "message": f"Error deleting persona: {str(e)}",
            "data": None
        }


# Bot Management Endpoints
@app.post("/bots", response_model=APIResponse)
async def create_bot(bot_create: BotCreate, background_tasks: BackgroundTasks):
    """Create and start new bot(s)"""
    try:
        # Validate count parameter
        try:
            bot_count = int(bot_create.count)
            if bot_count < 1 or bot_count > 5:
                return {
                    "success": False,
                    "message": f"Bot count must be between 1 and 5, got {bot_count}",
                    "data": None
                }
        except (ValueError, TypeError):
            return {
                "success": False,
                "message": f"Invalid bot count value: {bot_create.count}. Must be an integer between 1 and 5.",
                "data": None
            }
            
        # Reseed random for better randomness between API calls
        import random
        import time
        import os
        
        # Simple but effective random seeding
        random.seed(int(time.time() * 1000000) + int.from_bytes(os.urandom(4), byteorder='big'))
        
        # Check if we're creating multiple bots
        if bot_count > 1:
            # For multiple bots, we'll create them in sequence and return all the IDs
            created_bots = []
            
            # If no persona key is specified, select different personas for each bot
            selected_personas = []
            if not bot_create.persona_key:
                # Get available personas
                available_personas = persona_manager.list_personas()
                if not available_personas:
                    return {
                        "success": False,
                        "message": "No personas available. Please create a persona first.",
                        "data": None
                    }
                
                # Select random personas, ensuring we don't repeat if possible
                if len(available_personas) >= bot_count:
                    # We have enough unique personas - shuffle the list for better randomness
                    personas_copy = available_personas.copy()
                    random.shuffle(personas_copy)
                    selected_personas = personas_copy[:bot_count]
                else:
                    # Not enough unique personas, some will be repeated
                    for _ in range(bot_count):
                        selected_personas.append(random.choice(available_personas))
                        
                logger.info(f"Selected personas: {selected_personas}")
            else:
                # Use the same persona for all bots
                if bot_create.persona_key not in persona_manager.personas:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Persona with key {bot_create.persona_key} not found"
                    )
                selected_personas = [bot_create.persona_key] * bot_count
            
            # Create each bot
            for i in range(bot_count):
                # Create a single bot using the helper function
                dedup_suffix = f"instance-{i+1}"
                result = await _create_single_bot(
                    meeting_url=bot_create.meeting_url,
                    persona_key=selected_personas[i],
                    ngrok_url=bot_create.ngrok_url,
                    recorder_only=bot_create.recorder_only,
                    dedup_suffix=dedup_suffix,
                    background_tasks=background_tasks
                )
                
                if result["success"]:
                    created_bots.append(result["data"])
                else:
                    # If a bot creation fails, return what we've created so far along with error
                    return {
                        "success": False,
                        "message": f"Created {len(created_bots)} of {bot_count} bots successfully. Failed on bot {i+1}: {result['message']}",
                        "data": created_bots if created_bots else None
                    }
            
            # All bots created successfully
            return {
                "success": True,
                "message": f"Successfully created {len(created_bots)} bots",
                "data": created_bots
            }
        
        # Single bot creation - call the helper function
        return await _create_single_bot(
            meeting_url=bot_create.meeting_url,
            persona_key=bot_create.persona_key,
            ngrok_url=bot_create.ngrok_url,
            recorder_only=bot_create.recorder_only,
            dedup_suffix="",
            background_tasks=background_tasks
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error creating bot: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error creating bot: {str(e)}",
            "data": None
        }

async def _create_single_bot(
    meeting_url: str,
    persona_key: Optional[str],
    ngrok_url: Optional[str],
    recorder_only: bool,
    dedup_suffix: str,
    background_tasks: BackgroundTasks
) -> dict:
    """Helper function to create a single bot.
    
    Args:
        meeting_url: The URL of the meeting to join
        persona_key: Optional key of the persona to use
        ngrok_url: Optional WebSocket URL for the bot
        recorder_only: Whether the bot is a recorder only
        dedup_suffix: Suffix to make the deduplication key unique
        background_tasks: FastAPI background tasks object
        
    Returns:
        API response dictionary
    """
    try:
        # Handle persona selection
        selected_persona_key = persona_key
        if not selected_persona_key:
            # Select a random persona
            available_personas = persona_manager.list_personas()
            if not available_personas:
                return {
                    "success": False,
                    "message": "No personas available. Please create a persona first.",
                    "data": None
                }
            # Randomly select a persona (random has already been reseeded above)
            import random
            selected_persona_key = random.choice(available_personas)
            logger.info(f"Randomly selected persona: {selected_persona_key}")
        elif selected_persona_key not in persona_manager.personas:
            return {
                "success": False,
                "message": f"Persona with key {selected_persona_key} not found",
                "data": None
            }
        
        # Get persona details
        persona = persona_manager.get_persona(selected_persona_key)
        
        if recorder_only:
            # Create a recorder-only bot
            bot_id = meetingbaas.create_baas_bot(
                meeting_url=meeting_url,
                ngrok_url=None,
                persona_name=selected_persona_key,
                recorder_only=True,
                dedup_suffix=dedup_suffix
            )
            
            if not bot_id:
                return {
                    "success": False,
                    "message": "Failed to create recorder bot",
                    "data": None
                }
            
            # Store process info (even though there's no local process for recorder)
            active_bots[bot_id] = {
                "process": None,
                "persona_key": selected_persona_key,
                "meeting_url": meeting_url,
                "recorder_only": True
            }
            
            return {
                "success": True,
                "message": f"Recorder bot created with ID: {bot_id}",
                "data": {
                    "bot_id": bot_id,
                    "meeting_url": meeting_url,
                    "persona_key": selected_persona_key,
                    "status": "running",
                    "recorder_only": True
                }
            }
        
        # For a speaking bot, we need to:
        # 1. Start a bot process
        # 2. Start a proxy process
        # 3. Create an ngrok tunnel
        # 4. Create the MeetingBaas bot with the ngrok URL
        
        # Find available ports - use a function to find free ports
        def find_free_port(start_port):
            import socket
            port = start_port
            max_port = start_port + 100  # Try up to 100 ports
            
            while port < max_port:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.bind(('', port))
                        return port
                except OSError:
                    port += 1
            
            # If we get here, we couldn't find a free port
            raise RuntimeError(f"Could not find a free port between {start_port} and {max_port}")
        
        # Find free ports for bot and proxy
        bot_port = find_free_port(8765)  # Start with 8765
        proxy_port = find_free_port(bot_port + 1)  # Use the next free port after bot_port
        
        logger.info(f"Using ports: bot={bot_port}, proxy={proxy_port}")
        
        # Get the system prompt from the persona
        bot_prompt = persona.get("prompt", "")
        
        # Get voice ID from the persona or use a default
        voice_id = persona.get("cartesia_voice_id", "40104aff-a015-4da1-9912-af950fbec99e")
        
        # 1. Start the bot process
        bot_process = subprocess.Popen(
            [
                "poetry", "run", "bot",
                "-p", str(bot_port),
                "--system-prompt", bot_prompt,
                "--persona-name", selected_persona_key,
                "--voice-id", voice_id,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Set up output handling for the bot process
        def log_bot_output(stream, is_error=False):
            for line in iter(stream.readline, ''):
                line = line.strip()
                if line:
                    log_msg = f"[{selected_persona_key} bot] {line}"
                    if is_error:
                        logger.error(log_msg)
                    else:
                        logger.info(log_msg)
        
        # Start threads to capture and log the output
        threading.Thread(
            target=log_bot_output, 
            args=(bot_process.stdout,), 
            daemon=True
        ).start()
        
        threading.Thread(
            target=log_bot_output, 
            args=(bot_process.stderr, True), 
            daemon=True
        ).start()
        
        logger.info(f"Started bot process for {selected_persona_key}")
        
        # Wait a bit for the bot to initialize
        await asyncio.sleep(2)
        
        # 2. Start the proxy process
        proxy_process = subprocess.Popen(
            [
                "poetry", "run", "proxy",
                "-p", str(proxy_port),
                "--websocket-url", f"ws://localhost:{bot_port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Set up output handling for the proxy process
        def log_proxy_output(stream, is_error=False):
            for line in iter(stream.readline, ''):
                line = line.strip()
                if line:
                    log_msg = f"[{selected_persona_key} proxy] {line}"
                    if is_error:
                        logger.error(log_msg)
                    else:
                        logger.info(log_msg)
        
        # Start threads to capture and log the output
        threading.Thread(
            target=log_proxy_output, 
            args=(proxy_process.stdout,), 
            daemon=True
        ).start()
        
        threading.Thread(
            target=log_proxy_output, 
            args=(proxy_process.stderr, True), 
            daemon=True
        ).start()
        
        logger.info(f"Started proxy process for {selected_persona_key}")
        
        # Wait for the proxy to initialize properly
        await asyncio.sleep(5)  # Increased wait time to ensure proxy is fully initialized
        
        # 3. Create ngrok tunnel using a subprocess to ensure proper environment
        ngrok_process = subprocess.Popen(
            [
                "poetry", "run", "python", "-c",
                f"""
import asyncio
import ngrok
import os

async def create_tunnel():
    try:
        listener = await ngrok.forward({proxy_port}, authtoken_from_env=True)
        print(listener.url())
    except Exception as error:
        print(f"ERROR: {{error}}")
        exit(1)

asyncio.run(create_tunnel())
                """
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            universal_newlines=True
        )
        
        # Wait for ngrok to output the URL
        ngrok_output, ngrok_error = ngrok_process.communicate()
        
        if ngrok_process.returncode != 0 or "ERROR" in ngrok_output:
            logger.error(f"Failed to create ngrok tunnel: {ngrok_error if ngrok_error else ngrok_output}")
            # Clean up processes
            if bot_process and bot_process.poll() is None:
                bot_process.terminate()
            if proxy_process and proxy_process.poll() is None:
                proxy_process.terminate()
            return {
                "success": False,
                "message": f"Failed to create ngrok tunnel: {ngrok_error if ngrok_error else ngrok_output}",
                "data": None
            }
        
        # Get the URL from stdout
        ngrok_url = ngrok_output.strip()
        logger.info(f"Created ngrok tunnel: {ngrok_url}")
        
        # Convert http to ws URL if necessary
        if ngrok_url.startswith("https://"):
            websocket_url = "wss://" + ngrok_url[8:]
        else:
            websocket_url = "ws://" + ngrok_url[7:]
        
        # 4. Create the MeetingBaas bot
        bot_id = meetingbaas.create_baas_bot(
            meeting_url=meeting_url,
            ngrok_url=websocket_url,
            persona_name=selected_persona_key,
            recorder_only=False,
            dedup_suffix=dedup_suffix
        )
        
        if not bot_id:
            # Clean up processes
            if bot_process and bot_process.poll() is None:
                bot_process.terminate()
            if proxy_process and proxy_process.poll() is None:
                proxy_process.terminate()
            return {
                "success": False,
                "message": "Failed to create bot in MeetingBaas",
                "data": None
            }
        
        # Start a meetingbaas process to handle the connection
        meeting_process = subprocess.Popen(
            [
                "poetry", "run", "meetingbaas",
                "--meeting-url", meeting_url,
                "--persona-name", selected_persona_key,
                "--ngrok-url", websocket_url,
                "--dedup-suffix", dedup_suffix if dedup_suffix else "",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Set up output handling for the meetingbaas process
        def log_meeting_output(stream, is_error=False):
            for line in iter(stream.readline, ''):
                line = line.strip()
                if line:
                    log_msg = f"[{selected_persona_key} meetingbaas] {line}"
                    if is_error:
                        logger.error(log_msg)
                    else:
                        logger.info(log_msg)
        
        # Start threads to capture and log the output
        threading.Thread(
            target=log_meeting_output, 
            args=(meeting_process.stdout,), 
            daemon=True
        ).start()
        
        threading.Thread(
            target=log_meeting_output, 
            args=(meeting_process.stderr, True), 
            daemon=True
        ).start()
        
        logger.info(f"Started meetingbaas process for {selected_persona_key}")
        
        # Store all the process information
        active_bots[bot_id] = {
            "bot_process": bot_process,
            "proxy_process": proxy_process,
            "meeting_process": meeting_process,
            "ngrok_listener": None,
            "persona_key": selected_persona_key,
            "meeting_url": meeting_url,
            "recorder_only": False,
            "ports": {
                "bot": bot_port,
                "proxy": proxy_port
            }
        }
        
        return {
            "success": True,
            "message": f"Bot created with ID: {bot_id}",
            "data": {
                "bot_id": bot_id,
                "meeting_url": meeting_url,
                "persona_key": selected_persona_key,
                "status": "running",
                "recorder_only": False,
                "ngrok_url": websocket_url
            }
        }
    except Exception as e:
        logger.error(f"Error creating bot: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error creating bot: {str(e)}",
            "data": None
        }


@app.get("/bots", response_model=APIResponse)
async def list_bots():
    """List all active bots"""
    try:
        bots = []
        for bot_id, info in active_bots.items():
            bots.append({
                "bot_id": bot_id,
                "meeting_url": info["meeting_url"],
                "persona_key": info["persona_key"],
                "status": get_bot_status(bot_id),
                "recorder_only": info["recorder_only"]
            })
        
        return {
            "success": True,
            "message": f"Retrieved {len(bots)} bots",
            "data": bots
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error retrieving bots: {str(e)}",
            "data": None
        }


@app.get("/bots/{bot_id}", response_model=APIResponse)
async def get_bot(bot_id: str):
    """Get details for a specific bot"""
    try:
        if bot_id not in active_bots:
            raise HTTPException(
                status_code=404,
                detail=f"Bot with ID {bot_id} not found"
            )
        
        info = active_bots[bot_id]
        
        return {
            "success": True,
            "message": f"Retrieved bot: {bot_id}",
            "data": {
                "bot_id": bot_id,
                "meeting_url": info["meeting_url"],
                "persona_key": info["persona_key"],
                "status": get_bot_status(bot_id),
                "recorder_only": info["recorder_only"]
            }
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        return {
            "success": False,
            "message": f"Error retrieving bot: {str(e)}",
            "data": None
        }


@app.delete("/bots/{bot_id}", response_model=APIResponse)
async def delete_bot(bot_id: str):
    """Stop and delete a bot"""
    try:
        if bot_id not in active_bots:
            raise HTTPException(
                status_code=404,
                detail=f"Bot with ID {bot_id} not found"
            )
        
        # Get process information
        bot_info = active_bots[bot_id]
        
        # Clean up processes based on recorder_only flag
        if not bot_info["recorder_only"]:
            # Stop bot process if it's running
            if "bot_process" in bot_info and bot_info["bot_process"] and bot_info["bot_process"].poll() is None:
                logger.info(f"Stopping bot process for {bot_id}")
                bot_info["bot_process"].terminate()
                try:
                    bot_info["bot_process"].wait(timeout=5)
                except subprocess.TimeoutExpired:
                    bot_info["bot_process"].kill()
            
            # Stop proxy process if it's running
            if "proxy_process" in bot_info and bot_info["proxy_process"] and bot_info["proxy_process"].poll() is None:
                logger.info(f"Stopping proxy process for {bot_id}")
                bot_info["proxy_process"].terminate()
                try:
                    bot_info["proxy_process"].wait(timeout=5)
                except subprocess.TimeoutExpired:
                    bot_info["proxy_process"].kill()
                    
            # Stop meetingbaas process if it's running
            if "meeting_process" in bot_info and bot_info["meeting_process"] and bot_info["meeting_process"].poll() is None:
                logger.info(f"Stopping meetingbaas process for {bot_id}")
                bot_info["meeting_process"].terminate()
                try:
                    bot_info["meeting_process"].wait(timeout=5)
                except subprocess.TimeoutExpired:
                    bot_info["meeting_process"].kill()
        
        # Remove from active bots
        del active_bots[bot_id]
        
        # Delete the bot in MeetingBaas
        try:
            meetingbaas.delete_bot(bot_id)
        except Exception as e:
            logger.error(f"Error deleting bot from MeetingBaas: {e}")
            return {
                "success": False,
                "message": f"Bot processes were terminated but failed to delete from MeetingBaas: {str(e)}",
                "data": None
            }
        
        return {
            "success": True,
            "message": f"Deleted bot: {bot_id}",
            "data": None
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting bot: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": f"Error deleting bot: {str(e)}",
            "data": None
        }


# Image Generation Endpoint
@app.post("/personas/{key}/generate-image", response_model=APIResponse)
async def generate_persona_image(key: str, request: ImageGenerationRequest):
    """Generate an image for a persona"""
    try:
        from config.create_persona import generate_persona_image
        
        # Check if persona exists
        if key not in persona_manager.personas:
            raise HTTPException(
                status_code=404,
                detail=f"Persona with key {key} not found"
            )
        
        # Get API keys from request or environment
        replicate_key = request.replicate_key or os.getenv("REPLICATE_KEY")
        utfs_key = request.utfs_key or os.getenv("UTFS_KEY")
        app_id = request.app_id or os.getenv("APP_ID")
        
        if not all([replicate_key, utfs_key, app_id]):
            return {
                "success": False,
                "message": "Missing API keys for image generation",
                "data": None
            }
        
        # Generate image
        generate_persona_image(key, replicate_key, utfs_key, app_id)
        
        # Get updated persona with image URL
        updated_persona = persona_manager.get_persona(key)
        
        return {
            "success": True,
            "message": f"Generated image for persona: {key}",
            "data": {
                "key": key,
                "image_url": updated_persona.get("image", "")
            }
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        return {
            "success": False,
            "message": f"Error generating image: {str(e)}",
            "data": None
        }


# Health check endpoint
@app.get("/health", response_model=APIResponse)
async def health_check():
    """Health check endpoint"""
    return {
        "success": True,
        "message": "API is healthy",
        "data": {
            "active_bots": len(active_bots)
        }
    }


# Main execution
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True) 