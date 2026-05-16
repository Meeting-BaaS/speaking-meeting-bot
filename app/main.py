"""Main application module for the Speaking Meeting Bot API."""

import argparse
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()

# Prevent "duplicate file name frames.proto" crashes when running Pipecat in-process
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"


from fastapi import FastAPI, Request, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.api.routes import router as app_router
from app.api.routes_sales import sales_router
from app.api.websockets import websocket_router
from app.utils.pipecat_logger import configure_logger
from app.utils.ngrok import LOCAL_DEV_MODE, NGROK_URL_INDEX, NGROK_URLS, load_ngrok_urls

# Configure logging with the prettier logger
logger = configure_logger()
logger.name = "meetingbaas-api"  # Set logger name after configuring

# Set logging level for pipecat WebSocket client to WARNING to reduce noise
pipecat_ws_logger = logging.getLogger("pipecat.transports.network.websocket_client")
pipecat_ws_logger.setLevel(logging.WARNING)


async def api_key_middleware(request: Request, call_next):
    """Middleware to check for MeetingBaas API key in headers."""
    # Skip API key check for docs, openapi endpoints, and preflight OPTIONS requests
    if request.method == "OPTIONS" or request.url.path in ["/docs", "/openapi.json", "/redoc"]:
        return await call_next(request)

    api_key = request.headers.get("x-meeting-baas-api-key") or os.getenv("MEETING_BAAS_API_KEY")
    if not api_key:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"message": "Missing MeetingBaas API key in environment or headers"},
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
        version="0.0.1",
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

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include the routers
    app.include_router(app_router)
    app.include_router(sales_router)
    app.include_router(websocket_router)

    # Add a health endpoint
    @app.get("/health", tags=["system"])
    async def health():
        """Health check endpoint"""
        return {
            "status": "ok",
            "service": "speaking-meeting-bot",
            "version": "1.0.0",
            "endpoints": [
                # ── Legacy / original endpoints ──────────────────────────
                {
                    "path": "/bots",
                    "method": "POST",
                    "description": "Create a bot that joins a meeting (legacy)",
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
                # ── Sales-agent endpoints ─────────────────────────────────
                {
                    "path": "/run-bot",
                    "method": "POST",
                    "description": "Launch sales bot → joins meeting in passive mode",
                },
                {
                    "path": "/bot/{client_id}/engage",
                    "method": "POST",
                    "description": "Switch bot to active Q&A mode for 2-4 minutes",
                },
                {
                    "path": "/bot/{client_id}/report",
                    "method": "GET",
                    "description": "Retrieve meeting notes and extracted client needs",
                },
                {
                    "path": "/meeting-baas/webhook",
                    "method": "POST",
                    "description": "Receive MeetingBaaS event callbacks",
                },
            ],
        }

    return app


# Create the app instance for uvicorn
app = create_app()


def start_server(host: str = "0.0.0.0", port: int = 8000, local_dev: bool = False):
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

    # Set LOCAL_DEV_MODE based on parameter
    LOCAL_DEV_MODE = local_dev

    if local_dev:
        print("\n⚠️ Starting in local development mode")
        # Cache the ngrok URLs at server start
        import app.utils.ngrok as ngrok_utils
        ngrok_utils.NGROK_URLS = load_ngrok_urls()

        if ngrok_utils.NGROK_URLS:
            print(f"✅ {len(ngrok_utils.NGROK_URLS)} Bot(s) available from Ngrok")
            for i, url in enumerate(ngrok_utils.NGROK_URLS):
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
        "app.main:app",  # Point to the app instance in this module
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
        default=int(os.getenv("PORT", "8000")), # Read from env var, fallback to 8000
        help="Port to listen on",
    )
    parser.add_argument(
        "--local-dev",
        action="store_true",
        help="Run in local development mode with ngrok",
    )

    args = parser.parse_args()
    start_server(args.host, args.port, args.local_dev)
