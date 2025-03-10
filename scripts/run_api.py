#!/usr/bin/env python3
"""
Run the Speaking Meeting Bot API server.

This script starts the FastAPI server for the Speaking Meeting Bot API.
"""
import os
import sys
import argparse
from dotenv import load_dotenv
import uvicorn
from loguru import logger

# Add the parent directory to the path so we can import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meetingbaas_pipecat.utils.logger import configure_logger

# Load environment variables
load_dotenv()

# Configure logger
logger = configure_logger()


def main():
    """Run the API server."""
    parser = argparse.ArgumentParser(description="Run the Speaking Meeting Bot API server")
    parser.add_argument(
        "--host", 
        type=str, 
        default="0.0.0.0", 
        help="Host to bind the server to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", 
        type=int, 
        default=8000, 
        help="Port to bind the server to (default: 8000)"
    )
    parser.add_argument(
        "--reload", 
        action="store_true", 
        help="Enable auto-reload for development"
    )
    
    args = parser.parse_args()
    
    logger.info(f"Starting API server on {args.host}:{args.port}")
    logger.info("Press Ctrl+C to stop the server")
    
    uvicorn.run(
        "api.main:app", 
        host=args.host, 
        port=args.port, 
        reload=args.reload
    )


if __name__ == "__main__":
    main() 