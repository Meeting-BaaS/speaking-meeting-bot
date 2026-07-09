import json
from pathlib import Path

from app.main import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_committed_openapi_snapshot_matches_app_schema():
    snapshot = json.loads((ROOT / "speaking-bot-openapi.json").read_text())
    root_snapshot = json.loads((ROOT / "openapi.json").read_text())
    generated = create_app().openapi()

    assert snapshot == generated
    assert root_snapshot == generated


def test_openapi_snapshot_covers_prompt_context_mcp_and_speech_controls():
    snapshot = json.loads((ROOT / "speaking-bot-openapi.json").read_text())
    bot_props = snapshot["components"]["schemas"]["BotRequest"]["properties"]

    assert "prompt_data_sources" in bot_props
    assert "prompt_data_token_limit" in bot_props
    assert "mcp" in bot_props
    assert "mcp_profile" in bot_props
    assert "mcp_profile_tool_access" in bot_props
    assert "speech_speed" in bot_props
