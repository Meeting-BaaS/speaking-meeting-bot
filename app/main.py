"""Main application module for the Speaking Meeting Bot API."""

import argparse
import logging
import os
import sys

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.routes import router as app_router
from app.websockets import websocket_router
from meetingbaas_pipecat.utils.logger import configure_logger
from utils.runtime import build_public_base_url, parse_cors_origins
from utils.ngrok import LOCAL_DEV_MODE, NGROK_URL_INDEX, NGROK_URLS, load_ngrok_urls

# Configure logging with the prettier logger
logger = configure_logger()
logger.name = "meetingbaas-api"  # Set logger name after configuring
APP_VERSION = os.getenv("APP_VERSION", "0.1.0")

# Set logging level for pipecat WebSocket client to WARNING to reduce noise
pipecat_ws_logger = logging.getLogger("pipecat.transports.websocket.client")
pipecat_ws_logger.setLevel(logging.WARNING)


async def api_key_middleware(request: Request, call_next):
    """Middleware to check for MeetingBaas API key in headers."""
    # Skip API key check for docs and openapi endpoints.
    # /webhook is exempt too: MeetingBaas status callbacks (in_call_recording,
    # call_ended, …) don't carry our API key, and the handler only matches the
    # payload's bot_id against bots we launched ourselves. Blocking it meant
    # the ready signal never arrived and every bot sat mute through the full
    # 60s ready-wait timeout before speaking its entry message.
    if request.url.path in ["/docs", "/openapi.json", "/redoc", "/health", "/ready", "/", "/webhook"]:
        return await call_next(request)

    api_key = request.headers.get("x-meeting-baas-api-key")
    if not api_key:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"message": "Missing MeetingBaas API key in x-meeting-baas-api-key header"},
        )

    # Add the API key to the request state for use in routes
    request.state.api_key = api_key
    return await call_next(request)


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    Returns:
        A configured FastAPI application
    """
    app = FastAPI(
        title="Speaking Meeting Bot API",
        description="API for deploying AI-powered speaking agents in video meetings. Combines MeetingBaas for meeting connectivity with Pipecat for voice AI processing.",
        version=APP_VERSION,
        contact={
            "name": "Speaking Bot API by MeetingBaas",
            "url": "https://meetingbaas.com",
        },
        openapi_url="/openapi.json",  # Explicitly set the OpenAPI schema URL
        docs_url="/docs",  # Swagger UI path
        # redoc_url="/redoc",  # Explicitly set the ReDoc URL
    )

    # Add API key middleware
    app.middleware("http")(api_key_middleware)

    # Set the server URL for the OpenAPI schema
    app.openapi_schema = None  # Clear any existing schema

    # Override the openapi method to add server information
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema

        openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )

        # Ensure we extend, not replace, existing components
        components = openapi_schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["ApiKeyAuth"] = {
            "type": "apiKey",
            "in": "header",
            "name": "x-meeting-baas-api-key",
            "description": "MeetingBaas API key for authentication",
        }
        
        schemas = components.setdefault("schemas", {})
        schemas.update(
            {
                "PersonaImageRequest": {
                    "type": "object",
                    "required": ["name", "description"],
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the persona to generate an image for"
                        },
                        "description": {
                            "type": "string",
                            "description": "Detailed description of the persona's appearance and characteristics"
                        },
                        "gender": {
                            "type": "string",
                            "description": "Gender of the persona (optional)",
                            "enum": ["male", "female", "non-binary"]
                        },
                        "characteristics": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                            "description": "List of specific characteristics or features of the persona"
                        }
                    }
                },
                "PersonaImageResponse": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the persona"
                        },
                        "image_url": {
                            "type": "string",
                            "description": "URL of the generated image"
                        },
                        "generated_at": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Timestamp when the image was generated"
                        }
                    }
                }
            }
        )

        # Apply security globally
        openapi_schema["security"] = [{"ApiKeyAuth": []}]

        # Update the paths to include the required description parameter
        if "paths" in openapi_schema:
            if "/personas/generate-image" in openapi_schema["paths"]:
                openapi_schema["paths"]["/personas/generate-image"]["post"]["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/PersonaImageRequest"
                            }
                        }
                    }
                }

        openapi_schema["servers"] = [
            {
                "url": "https://speaking.meetingbaas.com",
                "description": "Production server",
            },
            {"url": "/", "description": "Local development server"},
        ]
        app.openapi_schema = openapi_schema
        return app.openapi_schema

    app.openapi = custom_openapi

    cors_origins = parse_cors_origins(os.getenv("CORS_ALLOW_ORIGINS"))
    allow_credentials = "*" not in cors_origins

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include the routers
    app.include_router(app_router)
    app.include_router(websocket_router)

    # Add a health endpoint
    @app.get("/health", tags=["system"])
    async def health():
        """Health check endpoint"""
        public_base_url = os.getenv("BASE_URL")
        return {
            "status": "ok",
            "service": "speaking-meeting-bot",
            "version": APP_VERSION,
            "public_base_url": public_base_url,
            "endpoints": [
                {
                    "path": "/bots",
                    "method": "POST",
                    "description": "Create a bot that joins a meeting",
                },
                {
                    "path": "/bots/{bot_id}",
                    "method": "DELETE",
                    "description": "Remove a bot using its bot ID",
                },
                {
                    "path": "/personas/generate-image",
                    "method": "POST",
                    "description": "Generate a persona image",
                },
                {"path": "/", "method": "GET", "description": "API root endpoint"},
                {
                    "path": "/health",
                    "method": "GET",
                    "description": "Health check endpoint",
                },
                {
                    "path": "/ws/{client_id}",
                    "method": "WebSocket",
                    "description": "WebSocket endpoint for client connections",
                },
                {
                    "path": "/pipecat/{client_id}",
                    "method": "WebSocket",
                    "description": "WebSocket endpoint for Pipecat connections",
                },
            ],
        }

    @app.get("/ready", tags=["system"])
    async def ready(request: Request):
        """Readiness endpoint with externally visible base URL resolution."""
        return {
            "status": "ready",
            "service": "speaking-meeting-bot",
            "version": APP_VERSION,
            "public_base_url": build_public_base_url(
                request, configured_base_url=os.getenv("BASE_URL")
            ),
            "cors": {
                "allow_origins": cors_origins,
                "allow_credentials": allow_credentials,
            },
        }

    return app


def start_server(host: str = "0.0.0.0", port: int = 7014, local_dev: bool = False):
    """Start the Uvicorn server for the FastAPI application."""
    # If the PORT environment variable is set, use it; otherwise, use the default.
    try:
        server_port = int(os.getenv("PORT", str(port)))
    except ValueError:
        logger.error(
            f"Invalid value for PORT environment variable: {os.getenv('PORT')}. "
            f"Falling back to default port {port}."
        )
        server_port = port
    logger.info(f"Starting server on {host}:{server_port}")

    # Global variables for ngrok URL tracking
    NGROK_URLS = []
    NGROK_URL_INDEX = 0

    # Set LOCAL_DEV_MODE based on parameter
    LOCAL_DEV_MODE = local_dev

    # Reset the ngrok URL counter when starting the server
    NGROK_URL_INDEX = 0

    if local_dev:
        print("\n⚠️ Starting in local development mode")
        # Cache the ngrok URLs at server start
        NGROK_URLS = load_ngrok_urls()

        if NGROK_URLS:
            print(f"✅ {len(NGROK_URLS)} Bot(s) available from Ngrok")
            for i, url in enumerate(NGROK_URLS):
                print(f"  Bot {i + 1}: {url}")
        else:
            print(
                "⚠️ No ngrok URLs configured. Using auto-detection for WebSocket URLs."
            )
        print("\n")

    logger.info(f"Starting WebSocket server on {host}:{server_port}")

    # Pass the local_dev flag as a command-line argument to the uvicorn process
    args = [
        sys.executable,
        "-m",
        "uvicorn",
        "app:app",  # Use the app from app/__init__.py
        "--host",
        host,
        "--port",
        str(server_port),
    ]

    if local_dev:
        args.extend(["--reload"])

        # Create a file that uvicorn will read on startup to set LOCAL_DEV_MODE
        with open(".local_dev_mode", "w") as f:
            f.write("true")
    else:
        # Make sure we don't have the flag set if not in local dev mode
        if os.path.exists(".local_dev_mode"):
            os.remove(".local_dev_mode")

    # Use os.execv to replace the current process with uvicorn
    # This way all arguments are directly passed to uvicorn
    os.execv(sys.executable, args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start the MeetingBaas Bot API server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "7014")), # Read from env var, fallback to 7014
        help="Port to listen on",
    )
    parser.add_argument(
        "--local-dev",
        action="store_true",
        help="Run in local development mode with ngrok",
    )

    args = parser.parse_args()
    start_server(args.host, args.port, args.local_dev)
