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


def test_openapi_snapshot_covers_representative_fields_and_endpoints():
    snapshot = json.loads((ROOT / "speaking-bot-openapi.json").read_text())
    bot_props = snapshot["components"]["schemas"]["BotRequest"]["properties"]

    for name in ("language", "callback_url", "callback_secret", "authority", "briefing"):
        assert name in bot_props, name
    assert bot_props["callback_secret"]["writeOnly"] is True
    for path in (
        "/bots/{bot_id}/messages",
        "/bots/{bot_id}/escalations",
        "/bots/{bot_id}/escalations/{escalation_id}/response",
        "/bots/{bot_id}/usage",
        "/bots/{bot_id}/briefing",
    ):
        assert path in snapshot["paths"], path


def test_openapi_snapshot_types_every_response_and_declares_callback_events():
    snapshot = json.loads((ROOT / "speaking-bot-openapi.json").read_text())
    schemas = snapshot["components"]["schemas"]

    leave = snapshot["paths"]["/bots/{bot_id}"]["delete"]["responses"]["200"]
    assert leave["content"]["application/json"]["schema"]["$ref"].endswith("/LeaveResponse")
    escalations = snapshot["paths"]["/bots/{bot_id}/escalations"]["get"]["responses"]["200"]
    assert escalations["content"]["application/json"]["schema"]["items"]["$ref"].endswith(
        "/Escalation"
    )

    expected = {
        "speaking_bot.utterance": "UtteranceEvent",
        "speaking_bot.escalation": "EscalationEvent",
        "speaking_bot.owner_message_delivered": "OwnerMessageDeliveredEvent",
        "speaking_bot.ended": "EndedEvent",
    }
    assert set(snapshot["webhooks"]) == set(expected)
    for event, model in expected.items():
        operation = snapshot["webhooks"][event]["post"]
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith(f"/{model}"), event
        assert "x-speaking-bot-signature" in operation["description"], event
        assert schemas[model]["properties"]["event"]["const"] == event
