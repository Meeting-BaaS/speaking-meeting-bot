"""API routes for the Speaking Meeting Bot application."""

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
import random

import openai

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.models import (
    BotRequest,
    JoinResponse,
    LeaveBotRequest,
    PersonaImageRequest,
    PersonaImageResponse,
)
from app.services.image_service import image_service
from config.persona_utils import persona_manager
from core.connection import MEETING_DETAILS, PIPECAT_PROCESSES, registry
from core.process import start_pipecat_process, terminate_process_gracefully
from core.router import router as message_router

# Import from the app module (will be defined in __init__.py)
from meetingbaas_pipecat.utils.logger import logger
import aiohttp

from scripts.meetingbaas_api import (
    MEETING_BAAS_API_URL,
    MeetingBaasError,
    create_meeting_bot,
    leave_meeting_bot,
)
from utils.ngrok import (
    LOCAL_DEV_MODE,
    determine_websocket_url,
    log_ngrok_status,
    release_ngrok_url,
    update_ngrok_client_id,
)
from utils.runtime import build_public_base_url, get_internal_pipecat_ws_url, get_state_dir
from config.prompts import PERSONA_INTERACTION_INSTRUCTIONS

# Import the new persona detail extraction service
from app.services.persona_detail_extraction import extract_persona_details_from_prompt

router = APIRouter()


@router.post(
    "/bots",
    tags=["bots"],
    response_model=JoinResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Bot successfully created and joined the meeting"},
        400: {"description": "Bad request - Missing required fields or invalid data"},
        500: {
            "description": "Server error - Failed to create bot through MeetingBaas API"
        },
    },
)
async def join_meeting(request: BotRequest, client_request: Request):
    """
    Create and deploy a speaking bot in a meeting.

    Launches an AI-powered bot that joins a video meeting through MeetingBaas
    and processes audio using Pipecat's voice AI framework.
    """
    # Validate required parameters
    if not request.meeting_url:
        return JSONResponse(
            content={"message": "Meeting URL is required", "status": "error"},
            status_code=400,
        )

    # Get API key from request state (set by middleware)
    api_key = client_request.state.api_key

    # Log local dev mode status
    if LOCAL_DEV_MODE:
        logger.info("🔍 Running in LOCAL_DEV_MODE - will prioritize ngrok URLs")
    else:
        logger.info("🔍 Running in standard mode")

    # Determine WebSocket URL (works in all cases now)
    websocket_url, temp_client_id = determine_websocket_url(
        request.websocket_url, client_request
    )

    logger.info(f"Starting bot for meeting {request.meeting_url}")
    logger.info(f"WebSocket URL: {websocket_url}")
    logger.info(f"Bot name: {request.bot_name}")

    # INTERNAL PARAMETER: Set a fixed value for streaming_audio_frequency
    # This is not exposed in the API and is always "16khz"
    streaming_audio_frequency = "16khz"
    logger.info(f"Using fixed streaming audio frequency: {streaming_audio_frequency}")

    # Set the converter sample rate based on our fixed streaming_audio_frequency
    from core.converter import converter

    sample_rate = 16000  # Always 16000 Hz for 16khz audio
    converter.set_sample_rate(sample_rate)
    logger.info(
        f"Set audio sample rate to {sample_rate} Hz for {streaming_audio_frequency}"
    )

    # Generate a unique client ID for this bot
    bot_client_id = str(uuid.uuid4())

    # If we're in local dev mode and we have a temp client ID, update the mapping
    if LOCAL_DEV_MODE and temp_client_id:
        update_ngrok_client_id(temp_client_id, bot_client_id)
        log_ngrok_status()

    # --- Streamlined Persona and Prompt Resolution Logic ---
    final_prompt: str = ""
    resolved_persona_data: Dict[str, Any] = {}
    persona_name_for_logging: str = "Unknown"

    if request.prompt: # Case 1: Custom prompt provided (dynamic persona)
        logger.info(f"Processing custom prompt for bot {bot_client_id}")
        prompt_derived_details = await extract_persona_details_from_prompt(request.prompt)

        if prompt_derived_details and isinstance(prompt_derived_details, dict): # Ensure it's a dict
            # Construct resolved_persona_data from derived details
            resolved_persona_data = {
                "name": prompt_derived_details.get("name", "Bot"),
                "prompt": request.prompt, # Store original request prompt as the base prompt for dynamic persona
                "description": prompt_derived_details.get("description", request.prompt), # Use derived description or fallback to full prompt
                "gender": prompt_derived_details.get("gender", "male"),
                "characteristics": prompt_derived_details.get("characteristics", []), # Ensure it's a list
                "image": None, # Will be generated/resolved later
                "cartesia_voice_id": None, # Will be matched later
                "relevant_links": [],
                "additional_content": None,
                "is_temporary": True # Mark as temporary persona
            }
            persona_name_for_logging = resolved_persona_data["name"]
            final_prompt = request.prompt + PERSONA_INTERACTION_INSTRUCTIONS
            logger.info(f"Dynamically created persona '{persona_name_for_logging}' from prompt.")
        else:
            # Fallback if prompt details extraction fails or returns unexpected type
            logger.warning("Failed to extract persona details from custom prompt or received unexpected type. Falling back to default bot persona.")
            resolved_persona_data = persona_manager.get_persona("baas_onboarder")
            resolved_persona_data["is_temporary"] = False # Ensure fallback is not marked temporary
            persona_name_for_logging = resolved_persona_data.get("name", "baas_onboarder")
            final_prompt = resolved_persona_data["prompt"] + PERSONA_INTERACTION_INSTRUCTIONS

    else: # Case 2: No custom prompt, use pre-defined persona
        resolved_persona_name: str

        # Priority: request.personas > request.bot_name > random > baas_onboarder
        if request.personas and len(request.personas) > 0:
            resolved_persona_name = request.personas[0]
            logger.info(f"Using specified persona '{resolved_persona_name}' for bot.")
        elif request.bot_name and request.bot_name in persona_manager.personas:
            resolved_persona_name = request.bot_name
            logger.info(f"Using bot_name as persona '{resolved_persona_name}' for bot.")
        else:
            available_personas = list(persona_manager.personas.keys())
            if available_personas:
                resolved_persona_name = random.choice(available_personas)
                logger.info(f"No persona specified, using random persona '{resolved_persona_name}' for bot.")
            else:
                resolved_persona_name = "baas_onboarder"
                logger.warning("No personas found, using fallback persona: baas_onboarder.")

        try:
            resolved_persona_data = persona_manager.get_persona(resolved_persona_name)
            resolved_persona_data["is_temporary"] = False # Mark as not temporary
            persona_name_for_logging = resolved_persona_data.get("name", resolved_persona_name)
            final_prompt = resolved_persona_data["prompt"] + PERSONA_INTERACTION_INSTRUCTIONS
            logger.info(f"Using pre-defined persona '{persona_name_for_logging}'.")
        except KeyError as e:
            logger.error(f"Resolved persona '{resolved_persona_name}' not found: {e}. Falling back to baas_onboarder.")
            resolved_persona_data = persona_manager.get_persona("baas_onboarder")
            resolved_persona_data["is_temporary"] = False # Ensure fallback is not marked temporary
            persona_name_for_logging = resolved_persona_data.get("name", "baas_onboarder")
            final_prompt = resolved_persona_data["prompt"] + PERSONA_INTERACTION_INSTRUCTIONS
            logger.info(f"Using fallback persona '{persona_name_for_logging}'.")

    # Populate image if not present
    if not resolved_persona_data.get("image"):
        image_prompt_desc = resolved_persona_data.get("description") or resolved_persona_data.get("prompt")
        if image_prompt_desc:
            logger.info(f"Attempting to generate image for '{persona_name_for_logging}' with prompt: {image_prompt_desc}")
            try:
                generated_image = await image_service.generate_persona_image(
                    name=resolved_persona_data.get("name", "Bot"), # Use persona's resolved name
                    prompt=image_prompt_desc,
                    style="cinematic, detailed, photorealistic, professional headshot",
                    size=(512, 512)
                )

                if generated_image:
                    resolved_persona_data["image"] = generated_image
                    logger.info(f"Generated image URL for '{persona_name_for_logging}': {generated_image}")
                else:
                    logger.warning(f"Image generation returned no URL.")
                    resolved_persona_data["image"] = None # Ensure no invalid image data is stored
            except Exception as e:
                logger.error(f"Failed to generate image for '{persona_name_for_logging}': {e}")

    # Populate voice ID if not present
    if not resolved_persona_data.get("cartesia_voice_id"):
        from config.voice_utils import VoiceUtils # Import here to avoid circular dependency issues
        voice_utils = VoiceUtils()
        cartesia_voice_id = await voice_utils.match_voice_to_persona(persona_details=resolved_persona_data) # Pass the whole dict
        resolved_persona_data["cartesia_voice_id"] = cartesia_voice_id
        logger.info(f"Resolved Cartesia voice ID for '{persona_name_for_logging}': {cartesia_voice_id}")

    logger.info(f"Final resolved persona data for Pipecat process:")
    logger.info(f"  Name: {resolved_persona_data.get('name')}")
    logger.info(f"  Image: {resolved_persona_data.get('image')}")
    logger.info(f"  Voice ID: {resolved_persona_data.get('cartesia_voice_id')}")
    logger.info(f"  Is Temporary: {resolved_persona_data.get('is_temporary')}")

    # Store all relevant details in MEETING_DETAILS dictionary.
    # Index 5 carries the FULL resolved persona dict (prompt, voice, image…):
    # the Pipecat child is spawned later from app/websockets.py when MeetingBaas
    # connects, and dynamic (prompt-derived) personas exist only in this dict —
    # they are never written to config/personas, so the child cannot re-resolve
    # them from disk.
    MEETING_DETAILS[bot_client_id] = (
        request.meeting_url,
        resolved_persona_data.get("name", persona_name_for_logging),  # Use display name from resolved data
        None,  # meetingbaas_bot_id, will be set after creation
        request.enable_tools,
        streaming_audio_frequency,
        resolved_persona_data,
    )

    # Get image URL: Prioritize request.bot_image > persona_data.image > generate_image (if custom prompt and details derived)
    bot_image = request.bot_image
    if not bot_image:
        # Check persona data first (whether existing or temporary)
        if resolved_persona_data.get("image"):
             # Ensure the image is a string
            try:
                bot_image = str(resolved_persona_data.get("image"))
                logger.info(f"Using persona image from resolved persona data: {bot_image}")
            except Exception as e:
                logger.error(f"Error converting persona image from resolved persona data to string: {e}")
                bot_image = None
        # Only attempt to generate image if custom prompt was originally used AND details were derived
        elif request.prompt and prompt_derived_details:
            logger.info("Attempting to generate image based on custom LLM prompt derived details...")
            # Use details from prompt_derived_details to create a PersonaImageRequest
            try:
                # Use derived details, falling back to defaults or original prompt where needed
                image_request_data = PersonaImageRequest(
                    name=prompt_derived_details.get("name", "Bot"), 
                    description=prompt_derived_details.get("description", request.prompt), 
                    gender=prompt_derived_details.get("gender", "male"), 
                    characteristics=prompt_derived_details.get("characteristics", [])
                )
                # Construct a more detailed prompt for the image service if possible
                image_prompt_desc = image_request_data.description
                if image_request_data.gender:
                   image_prompt_desc = f"{image_request_data.gender.capitalize()}. {image_prompt_desc}"
                if image_request_data.characteristics:
                   traits = ", ".join(image_request_data.characteristics)
                   image_prompt_desc = f"{image_prompt_desc}. With features like {traits}"

                # Add standard quality guidelines
                image_prompt_desc += ". High quality, single person, only face and shoulders, centered, neutral background, avoid borders."

                generated_image_url = await image_service.generate_persona_image(
                    name=image_request_data.name,
                    prompt=image_prompt_desc, 
                    style="realistic", 
                    size=(512, 512) 
                )
                if generated_image_url:
                    bot_image = generated_image_url
                    logger.info(f"Generated image: {bot_image}")
                else:
                    logger.error(f"Failed to generate image from prompt derived details: No URL returned.")
                    bot_image = None
            except Exception as e:
                logger.error(f"Failed to generate image from prompt derived details: {e}")
                bot_image = None

    # Ensure the bot_image is definitely a string or None before passing to 'create_meeting_bot
    bot_image_str = str(bot_image) if bot_image is not None else None
    if bot_image_str is not None:
         logger.info(f"Final bot image URL: {bot_image_str}")
    else:
         logger.info("No bot image URL resolved.")

    # Determine the final entry message
    final_entry_message: Optional[str] = request.entry_message
    if not final_entry_message and resolved_persona_data.get("entry_message"):
        final_entry_message = resolved_persona_data.get("entry_message")
    elif not final_entry_message and resolved_persona_data.get("is_temporary", False):
        # For temporary personas without a specified entry_message, provide a dynamic default
        final_entry_message = f"Hello, I'm {persona_name_for_logging}, ready to assist you throughout this session."

    # The Pipecat child reads the entry message from the persona dict it gets
    # handed (core/process.py doesn't pass --entry-message), so the resolved
    # request-level message must live there — otherwise the child falls back
    # to the persona's on-disk message or a generic default.
    resolved_persona_data["entry_message"] = final_entry_message

    # Same channel for per-request turn-taking tuning: the child applies these
    # over its VAD_* env defaults.
    if request.turn_config:
        resolved_persona_data["turn_config"] = request.turn_config.model_dump(
            exclude_none=True
        )

    # Create bot directly through MeetingBaas API
    # Use persona display name from resolved_persona_data for MeetingBaas API call
    # Use the websocket_url as the webhook_url (same base URL, different endpoint)
    public_base_url = build_public_base_url(
        client_request, configured_base_url=os.getenv("BASE_URL")
    )
    webhook_url = f"{public_base_url}/webhook"
    try:
        meetingbaas_bot_id = create_meeting_bot(
            meeting_url=request.meeting_url,
            websocket_url=websocket_url,
            bot_id=bot_client_id,
            persona_name=resolved_persona_data.get("name", persona_name_for_logging),  # Use resolved display name
            api_key=api_key,
            bot_image=bot_image_str,  # Use the pre-stringified value
            entry_message=final_entry_message,
            extra=request.extra,
            streaming_audio_frequency=streaming_audio_frequency,
            webhook_url=webhook_url,
        )
    except MeetingBaasError as e:
        # Surface the upstream rejection (409 bot-already-exists, 401 bad key, …)
        # instead of a generic 500 the caller can't act on.
        MEETING_DETAILS.pop(bot_client_id, None)
        return JSONResponse(
            content={
                "message": f"MeetingBaas API rejected the bot: {e.message}",
                "status": "error",
                "upstream_status": e.status_code,
            },
            status_code=e.status_code if 400 <= e.status_code < 600 else 502,
        )

    if meetingbaas_bot_id:
        # Update the meetingbaas_bot_id in MEETING_DETAILS
        # Convert tuple to list to allow assignment
        details = list(MEETING_DETAILS[bot_client_id])
        details[2] = meetingbaas_bot_id
        MEETING_DETAILS[bot_client_id] = tuple(details)

        # Log the client_id for internal reference
        logger.info(f"Bot created with MeetingBaas bot_id: {meetingbaas_bot_id}")
        logger.info(f"Internal client_id for WebSocket connections: {bot_client_id}")

        # Start the Pipecat process as a subprocess
        # The Pipecat process should connect to our LOCAL WebSocket server, not the external one
        pipecat_websocket_url = get_internal_pipecat_ws_url(bot_client_id)
        process = start_pipecat_process(
            client_id=bot_client_id,
            websocket_url=pipecat_websocket_url,  # Use internal URL, not external
            meeting_url=request.meeting_url,
            persona_data=resolved_persona_data,
            streaming_audio_frequency=streaming_audio_frequency,
            enable_tools=request.enable_tools,
            api_key=api_key,
            meetingbaas_bot_id=meetingbaas_bot_id,
        )

        # Store the process for later termination
        PIPECAT_PROCESSES[bot_client_id] = process

        # Poll the bot's lifecycle status until it is actually in the call and
        # write the ready signal then. Complements the roster heuristic in
        # app/websockets.py: per-bot callbacks only fire on completion/failure,
        # so GET /v2/bots/{id}/status is the API-only way to see
        # in_waiting_room → in_call_recording transitions.
        asyncio.create_task(
            _poll_bot_admission(meetingbaas_bot_id, bot_client_id, api_key)
        )

        # Return only the bot_id in the response
        return JoinResponse(bot_id=meetingbaas_bot_id)
    else:
        # Clean up MEETING_DETAILS if bot creation failed
        if bot_client_id in MEETING_DETAILS:
             MEETING_DETAILS.pop(bot_client_id)

        return JSONResponse(
            content={
                "message": "Failed to create bot through MeetingBaas API",
                "status": "error",
            },
            status_code=500,
        )


@router.delete(
    "/bots/{bot_id}",
    tags=["bots"],
    response_model=Dict[str, Any],
    responses={
        200: {"description": "Bot successfully removed from meeting"},
        400: {"description": "Bad request - Missing required fields or identifiers"},
        404: {"description": "Bot not found - No bot with the specified ID"},
        500: {
            "description": "Server error - Failed to remove bot from MeetingBaas API"
        },
    },
)
async def leave_bot(
    bot_id: str,
    request: LeaveBotRequest,
    client_request: Request,
):
    """
    Remove a bot from a meeting by its ID.

    This will:
    1. Call the MeetingBaas API to make the bot leave
    2. Close WebSocket connections if they exist
    3. Terminate the associated Pipecat process
    """
    logger.info(f"Removing bot with ID: {bot_id}")
    # Get API key from request state (set by middleware)
    api_key = client_request.state.api_key

    # Verify we have the bot_id
    if not bot_id and not request.bot_id:
        return JSONResponse(
            content={
                "message": "Bot ID is required",
                "status": "error",
            },
            status_code=400,
        )

    # Use the path parameter bot_id if provided, otherwise use request.bot_id
    meetingbaas_bot_id = bot_id or request.bot_id
    client_id = None

    # Look through MEETING_DETAILS to find the client ID for this bot ID
    for cid, details in MEETING_DETAILS.items():
        # Check if the stored meetingbaas_bot_id matches
        if details[2] == meetingbaas_bot_id: # Accessing tuple element by index
            client_id = cid
            logger.info(f"Found client ID {client_id} for bot ID {meetingbaas_bot_id}")
            break

    if not client_id:
        logger.warning(f"No client ID found for bot ID {meetingbaas_bot_id}")

    success = True

    # 1. Call MeetingBaas API to make the bot leave
    if meetingbaas_bot_id:
        logger.info(f"Removing bot with ID: {meetingbaas_bot_id} from MeetingBaas API")
        result = leave_meeting_bot(
            bot_id=meetingbaas_bot_id,
            api_key=api_key,
        )
        if not result:
            success = False
            logger.error(
                f"Failed to remove bot {meetingbaas_bot_id} from MeetingBaas API"
            )
    else:
        logger.warning("No MeetingBaas bot ID or API key found, skipping API call")

    # 2. Close WebSocket connections if they exist
    if client_id:
        # Mark the client as closing to prevent further messages
        message_router.mark_closing(client_id)

        # Close Pipecat WebSocket first
        if client_id in registry.pipecat_connections:
            try:
                await registry.disconnect(client_id, is_pipecat=True)
                logger.info(f"Closed Pipecat WebSocket for client {client_id}")
            except Exception as e:
                success = False
                logger.error(f"Error closing Pipecat WebSocket: {e}")

        # Then close client WebSocket if it exists
        if client_id in registry.active_connections:
            try:
                await registry.disconnect(client_id, is_pipecat=False)
                logger.info(f"Closed client WebSocket for client {client_id}")
            except Exception as e:
                success = False
                logger.error(f"Error closing client WebSocket: {e}")

        # Add a small delay to allow for clean disconnection
        await asyncio.sleep(0.5)

    # 3. Terminate the Pipecat process after WebSockets are closed
    if client_id and client_id in PIPECAT_PROCESSES:
        process = PIPECAT_PROCESSES[client_id]
        if process and process.poll() is None:  # If process is still running
            try:
                if terminate_process_gracefully(process, timeout=3.0):
                    logger.info(
                        f"Gracefully terminated Pipecat process for client {client_id}"
                    )
                else:
                    logger.warning(
                        f"Had to forcefully kill Pipecat process for client {client_id}"
                    )
            except Exception as e:
                success = False
                logger.error(f"Error terminating Pipecat process: {e}")

        # Remove from our storage
        PIPECAT_PROCESSES.pop(client_id, None)

        # Clean up meeting details
        if client_id in MEETING_DETAILS:
            MEETING_DETAILS.pop(client_id, None)

        # Release ngrok URL if in local dev mode
        if LOCAL_DEV_MODE and client_id:
            release_ngrok_url(client_id)
            log_ngrok_status()
    else:
        logger.warning(f"No Pipecat process found for client {client_id}")

    return {
        "message": "Bot removal request processed",
        "status": "success" if success else "partial",
        "bot_id": meetingbaas_bot_id,
    }


@router.post(
    "/personas/generate-image",
    tags=["personas"],
    response_model=PersonaImageResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Image successfully generated"},
        400: {"description": "Invalid request data"},
    },
)
async def generate_persona_image(request: PersonaImageRequest) -> PersonaImageResponse:
    """Generate an image for a persona using Replicate."""
    try:
        # Build the prompt from available fields
        # Build the prompt using a more concise approach
        name = request.name
        prompt = f"A detailed professional portrait of a single person named {name}"

        if request.gender:
            prompt += f". {request.gender.capitalize()}"

        if request.description:
            cleaned_desc = request.description.strip().rstrip(".")
            prompt += f". Who {cleaned_desc}"

        if request.characteristics and len(request.characteristics) > 0:
            traits = ", ".join(request.characteristics)
            prompt += f". With features like {traits}"

        # Add standard quality guidelines
        prompt += ". High quality, single person, only face and shoulders, centered, neutral background, avoid borders."

        # Generate the image
        image_generation_result = await image_service.generate_persona_image(
            name=name, prompt=prompt, style="realistic", size=(512, 512)
        )

        if not image_generation_result: # Check if the string is empty/None
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to generate image: No URL returned."
            )

        image_url = image_generation_result # Use the string directly

        return PersonaImageResponse(
            name=name,
            image_url=image_url,
            generated_at=datetime.utcnow(),
        )

    except Exception as e:
        logger.error(f"Error generating image: {str(e)}")
        if isinstance(e, ValueError):
            # ValueError typically indicates invalid input
            status_code = status.HTTP_400_BAD_REQUEST
        elif "connection" in str(e).lower() or "timeout" in str(e).lower():
            # Network errors should be 503 Service Unavailable
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            # Default to internal server error
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        raise HTTPException(status_code=status_code, detail=str(e))


# Directory for signaling between webhook and Pipecat process
READY_SIGNALS_DIR = os.path.join(get_state_dir(), "ready_signals")

# Bot lifecycle codes from GET /v2/bots/{id}/status that mean "in the call"
# (release the entry message) vs "over" (stop polling).
_IN_CALL_STATUSES = {
    "in_call_recording",
    "in_call_not_recording",
    "recording_paused",
    "recording_resumed",
}
_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "call_ended",
    "recording_succeeded",
    "recording_failed",
    "transcribing",
    "transcription_failed",
    "awaiting_reconciliation",
}


async def _poll_bot_admission(
    meetingbaas_bot_id: str, client_id: str, api_key: str, max_wait_secs: int = 900
) -> None:
    """Poll the bot's status until it's in the call, then write its ready file.

    Per-bot callbacks only deliver completion/failure, and the status-change
    webhook stream requires account-level SVIX configuration — polling
    GET /v2/bots/{id}/status is the pure-API way to know when a lobbied bot
    is actually admitted. Also logs lifecycle transitions (in_waiting_room …)
    for observability.
    """
    url = f"{MEETING_BAAS_API_URL}/v2/bots/{meetingbaas_bot_id}/status"
    headers = {"x-meeting-baas-api-key": api_key}
    last_status = None
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(max_wait_secs // 2):
                try:
                    async with session.get(
                        url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            body = await resp.json()
                            bot_status = body.get("data", {}).get("status")
                            if bot_status != last_status:
                                logger.info(
                                    f"Bot {meetingbaas_bot_id} lifecycle: {bot_status}"
                                )
                                last_status = bot_status
                            if bot_status in _IN_CALL_STATUSES:
                                os.makedirs(READY_SIGNALS_DIR, exist_ok=True)
                                ready_file = os.path.join(
                                    READY_SIGNALS_DIR, f"{client_id}.ready"
                                )
                                with open(ready_file, "w") as f:
                                    f.write(datetime.now().isoformat())
                                logger.info(
                                    f"Bot {meetingbaas_bot_id} admitted — ready signal written for {client_id}"
                                )
                                return
                            if bot_status in _TERMINAL_STATUSES:
                                logger.info(
                                    f"Bot {meetingbaas_bot_id} reached terminal status "
                                    f"'{bot_status}' before admission — stopping poll"
                                )
                                return
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.debug(f"Status poll error for {meetingbaas_bot_id}: {e}")
                await asyncio.sleep(2)
        logger.warning(
            f"Bot {meetingbaas_bot_id} not admitted within {max_wait_secs}s — poll ended"
        )
    except Exception as e:
        logger.error(f"Status poller crashed for {meetingbaas_bot_id}: {e}")


@router.post(
    "/webhook",
    tags=["webhook"],
    status_code=status.HTTP_200_OK,
)
async def meetingbaas_webhook(request: Request):
    """
    Webhook endpoint for MeetingBaas callbacks.

    Receives events like bot_joined, bot_left, call_ended, transcription, etc.
    - On 'in_call_recording': signals Pipecat to start speaking
    - On call end: generates a summary from the transcript
    """
    try:
        body = await request.json()
        logger.info(f"Received MeetingBaas webhook: {body}")

        # Extract event info - MeetingBaaS uses nested structure:
        # {'event': 'bot.status_change', 'data': {'bot_id': '...', 'status': {'code': 'in_call_recording'}}}
        event_type = body.get("event") or body.get("type") or ""
        data = body.get("data", {})
        bot_id = body.get("bot_id") or body.get("botId") or data.get("bot_id")

        # The actual status code is nested in data.status.code for status_change events
        status_code = ""
        if isinstance(data.get("status"), dict):
            status_code = data["status"].get("code", "")

        # Combine event type and status code for checking
        event_type_lower = str(event_type).lower()
        status_code_lower = str(status_code).lower()

        logger.info(f"Webhook event: {event_type}, status_code: {status_code}, bot_id: {bot_id}")

        # Check for "in_call_not_recording" or "in_call_recording" - bot can speak as soon as it's in the call
        ready_events = ["in_call_not_recording", "in_call_recording", "recording_started", "bot_ready"]

        # Check both event_type and status_code for ready events
        is_ready_event = (
            any(ready in event_type_lower for ready in ready_events) or
            any(ready in status_code_lower for ready in ready_events)
        )

        if is_ready_event:
            logger.info(f"Bot {bot_id} is ready (in_call_recording) - signaling Pipecat to start speaking")

            # Find the internal client_id for this bot
            internal_client_id = None
            for internal_id, details in MEETING_DETAILS.items():
                if len(details) > 2 and details[2] == bot_id:
                    internal_client_id = internal_id
                    break

            if internal_client_id:
                # Write ready signal file
                os.makedirs(READY_SIGNALS_DIR, exist_ok=True)
                ready_file = os.path.join(READY_SIGNALS_DIR, f"{internal_client_id}.ready")
                with open(ready_file, "w") as f:
                    f.write(datetime.now().isoformat())
                logger.info(f"Created ready signal for client {internal_client_id}")
            else:
                logger.warning(f"Could not find internal client_id for bot {bot_id}")

        # Check for call ended events
        end_events = ["call_ended", "bot_left", "meeting_ended", "complete"]

        # Check both event_type and status_code for end events
        is_end_event = (
            any(end in event_type_lower for end in end_events) or
            any(end in status_code_lower for end in end_events)
        )

        if is_end_event:
            logger.info(f"Call ended event detected for bot {bot_id}, generating summary...")

            # Try to find the transcript file
            # First try with the MeetingBaaS bot_id, then look for any matching internal client_id
            transcript_dir = os.path.join(get_state_dir(), "transcripts")
            transcript_file = None

            if bot_id and os.path.exists(os.path.join(transcript_dir, f"{bot_id}.json")):
                transcript_file = os.path.join(transcript_dir, f"{bot_id}.json")
            else:
                # Try to find by looking up internal client_id from MEETING_DETAILS
                for internal_id, details in MEETING_DETAILS.items():
                    if len(details) > 2 and details[2] == bot_id:
                        potential_file = os.path.join(transcript_dir, f"{internal_id}.json")
                        if os.path.exists(potential_file):
                            transcript_file = potential_file
                            break

                # If still not found, try to find any recent transcript
                if not transcript_file and os.path.exists(transcript_dir):
                    files = sorted(os.listdir(transcript_dir), key=lambda x: os.path.getmtime(os.path.join(transcript_dir, x)), reverse=True)
                    for f in files:
                        if f.endswith('.json'):
                            transcript_file = os.path.join(transcript_dir, f)
                            break

            if transcript_file and os.path.exists(transcript_file):
                await generate_summary_from_transcript(transcript_file, bot_id)
            else:
                logger.warning(f"No transcript file found for bot {bot_id}")

        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return {"status": "error", "message": str(e)}


async def generate_summary_from_transcript(transcript_file: str, bot_id: str = None):
    """Generate a call summary from a transcript file using OpenAI.

    Uses bot_id in filename to prevent duplicate summaries when multiple
    call_ended webhooks are received for the same bot.
    """
    try:
        # Check if summary already exists for this bot_id
        summaries_dir = os.path.join(get_state_dir(), "call_summaries")
        os.makedirs(summaries_dir, exist_ok=True)

        if bot_id:
            # Check if a summary file for this bot_id already exists
            existing_summary = os.path.join(summaries_dir, f"{bot_id}.md")
            if os.path.exists(existing_summary):
                logger.info(f"[WEBHOOK] Summary already exists for bot {bot_id}, skipping generation")
                return

        with open(transcript_file, "r") as f:
            transcript_data = json.load(f)

        messages = transcript_data.get("messages", [])
        persona_name = transcript_data.get("persona_name", "Bot")

        if not messages or len(messages) < 2:
            logger.info("Not enough messages in transcript to generate summary")
            return

        # Format the conversation for the summary prompt
        conversation_text = ""
        for msg in messages:
            role = "Bot" if msg["role"] == "assistant" else "Prospect"
            content = msg.get("content", "")
            if content:
                conversation_text += f"{role}: {content}\n"

        # Generate summary using OpenAI
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        summary_prompt = f"""You are analyzing a sales discovery call transcript. Please extract the following information and provide a structured summary:

TRANSCRIPT:
{conversation_text}

Please provide:
1. Prospect Name (if mentioned)
2. Company Name (if mentioned)
3. Summary of the conversation (key points discussed, pain points, current situation, goals)
4. Qualification Status (yes/no/maybe - based on whether they seem like a good fit)
5. Recommended Next Steps

Format your response as JSON with these keys: prospect_name, company_name, summary, qualified, next_steps"""

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that analyzes sales call transcripts. Always respond with valid JSON."},
                {"role": "user", "content": summary_prompt}
            ],
            temperature=0.3,
        )

        summary_text = response.choices[0].message.content

        # Try to parse as JSON, otherwise use as-is
        try:
            # Clean up the response if it has markdown code blocks
            if "```json" in summary_text:
                summary_text = summary_text.split("```json")[1].split("```")[0]
            elif "```" in summary_text:
                summary_text = summary_text.split("```")[1].split("```")[0]

            summary_data = json.loads(summary_text)
        except json.JSONDecodeError:
            summary_data = {
                "prospect_name": "Unknown",
                "company_name": "Unknown",
                "summary": summary_text,
                "qualified": "unknown",
                "next_steps": "Review transcript manually"
            }

        # Save the summary - use bot_id in filename to prevent duplicates
        # summaries_dir already created at top of function
        if bot_id:
            filename = f"{bot_id}.md"
        else:
            # Fallback to timestamp + prospect name if no bot_id
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            prospect_name = summary_data.get("prospect_name", "Unknown")
            safe_name = "".join(c if c.isalnum() else "_" for c in prospect_name)
            filename = f"{timestamp}_{safe_name}.md"
        filepath = os.path.join(summaries_dir, filename)

        content = f"""# Discovery Call Summary (Auto-Generated)

**Date:** {datetime.now().strftime("%Y-%m-%d %H:%M")}
**Prospect:** {summary_data.get("prospect_name", "Unknown")}
**Company:** {summary_data.get("company_name", "Unknown")}
**Qualified:** {summary_data.get("qualified", "Unknown")}
**Bot Persona:** {persona_name}

## Summary
{summary_data.get("summary", "No summary available")}

## Next Steps
{summary_data.get("next_steps", "No next steps identified")}

---
*This summary was auto-generated from the call transcript when the meeting ended.*
"""

        with open(filepath, "w") as f:
            f.write(content)

        logger.info(f"[WEBHOOK] Generated call summary: {filepath}")

        # Optionally, clean up the transcript file
        # os.remove(transcript_file)

    except Exception as e:
        logger.error(f"[WEBHOOK] Error generating summary from transcript: {e}")
