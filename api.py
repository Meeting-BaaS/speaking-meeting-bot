import os
import sys
import asyncio
from typing import Optional, List

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

from scripts.batch import BotProxyManager

app = FastAPI()

class BotRequest(BaseModel):
    count: int
    meeting_url: HttpUrl
    personas: Optional[str] = None
    add_recorder: Optional[bool] = False
    start_port: Optional[int] = 8765  

@app.post("/run-bots")
async def run_bots(req: BotRequest, x_meeting_baas_api_key: str = Header(..., alias="x-meeting-baas-api-key")):
    
    os.environ["MEETING_BAAS_API_KEY"] = x_meeting_baas_api_key
    manager = BotProxyManager()

    sys.argv = [
        "batch.py",
        "-c", str(req.count),
        "--meeting-url", str(req.meeting_url),
    ]

    if req.personas:
        sys.argv += ["--personas"] + req.personas.split()

    if req.add_recorder:
        sys.argv.append("--add-recorder")

    if req.start_port and req.start_port != 8765:
        sys.argv += ["--start-port", str(req.start_port)]

    # Run the bot logic in background
    asyncio.create_task(manager.async_main())

    return {
        "message": f"Started {req.count} bot(s)", 
        "status": "launched"
    }