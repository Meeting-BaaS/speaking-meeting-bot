"""
Testing Agent — Core route tests for the Speaking Meeting Bot API.
Covers: /health, /bots (validation), session_manager, and message router.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────────────────────
# App fixture
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    """TestClient with API-key middleware bypassed for unit tests."""
    from app.main import create_app
    app = create_app()

    # Remove middleware that requires real env vars in tests
    app.middleware_stack = None  # Reset so TestClient builds fresh

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────

def test_health_endpoint(client):
    """GET /health returns 200 with status=ok."""
    resp = client.get("/health", headers={"x-meeting-baas-api-key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "speaking-meeting-bot"


# ─────────────────────────────────────────────────────────────────────────────
# POST /bots — validation
# ─────────────────────────────────────────────────────────────────────────────

def test_post_bots_missing_meeting_url(client):
    """POST /bots without meeting_url returns 4xx."""
    resp = client.post(
        "/bots",
        json={"bot_name": "TestBot"},
        headers={"x-meeting-baas-api-key": "test-key"},
    )
    # FastAPI returns 422 for missing required fields
    assert resp.status_code in (400, 422)


def test_post_bots_empty_meeting_url(client):
    """POST /bots with empty meeting_url returns 400."""
    with patch("app.routes.create_meeting_bot", return_value=None), \
         patch("app.routes.determine_websocket_url", return_value=("ws://localhost/ws/test", None)), \
         patch("app.routes.extract_persona_details_from_prompt", new_callable=AsyncMock, return_value=None), \
         patch("app.routes.persona_manager") as mock_pm:
        mock_pm.get_persona.return_value = {
            "name": "Bot", "prompt": "You are a bot.", "description": "",
            "gender": "male", "characteristics": [], "image": None,
            "cartesia_voice_id": None, "relevant_links": [], "additional_content": None,
        }
        mock_pm.personas = {"baas_onboarder": {}}

        resp = client.post(
            "/bots",
            json={"meeting_url": "", "bot_name": "TestBot"},
            headers={"x-meeting-baas-api-key": "test-key"},
        )
        assert resp.status_code == 400
        assert "required" in resp.json().get("message", "").lower()


# ─────────────────────────────────────────────────────────────────────────────
# SessionManager unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_session_manager_store_and_get():
    from app.services.session_manager import SessionManager
    sm = SessionManager()
    session = sm.store_session(
        client_id="cid-1",
        bot_id="bid-1",
        meeting_url="https://meet.google.com/test",
        marketing_person_email="rep@co.com",
        client_name="Acme",
    )
    assert session.client_id == "cid-1"
    assert session.mode == "passive"

    fetched = sm.get_session("cid-1")
    assert fetched is session

    by_bot = sm.get_session_by_bot_id("bid-1")
    assert by_bot is session


def test_session_manager_add_note():
    from app.services.session_manager import SessionManager
    sm = SessionManager()
    sm.store_session("c1", "b1", "url", "rep@co.com", "Client")
    assert sm.add_note("c1", "First note") is True
    assert sm.add_note("nonexistent", "note") is False
    assert sm.get_session("c1").notes == ["First note"]


def test_session_manager_add_transcription():
    from app.services.session_manager import SessionManager
    sm = SessionManager()
    sm.store_session("c2", "b2", "url", "rep@co.com", "Client")
    sm.add_transcription("c2", "Alice", "Hello everyone")
    transcriptions = sm.get_session("c2").transcriptions
    assert len(transcriptions) == 1
    assert transcriptions[0]["speaker"] == "Alice"
    assert transcriptions[0]["text"] == "Hello everyone"


def test_session_manager_mode_transitions():
    from app.services.session_manager import SessionManager
    sm = SessionManager()
    sm.store_session("c3", "b3", "url", "rep@co.com", "Client")
    sm.set_mode("c3", "active")
    session = sm.get_session("c3")
    assert session.mode == "active"
    assert session.engaged_at is not None

    sm.set_mode("c3", "ended")
    assert session.mode == "ended"
    assert session.ended_at is not None


def test_session_manager_remove_session():
    from app.services.session_manager import SessionManager
    sm = SessionManager()
    sm.store_session("c4", "b4", "url", "rep@co.com", "Client")
    sm.remove_session("c4")
    assert sm.get_session("c4") is None
    assert sm.get_session_by_bot_id("b4") is None


def test_session_to_dict():
    from app.services.session_manager import SessionManager
    sm = SessionManager()
    session = sm.store_session("c5", "b5", "url", "rep@co.com", "Client")
    d = session.to_dict()
    assert d["client_id"] == "c5"
    assert d["bot_id"] == "b5"
    assert d["mode"] == "passive"
    assert "created_at" in d


# ─────────────────────────────────────────────────────────────────────────────
# ConnectionRegistry unit tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connection_registry_connect_client():
    from app.core.connection import ConnectionRegistry
    registry = ConnectionRegistry()
    ws = AsyncMock()
    ws.accept = AsyncMock()
    await registry.connect(ws, "client-1", is_pipecat=False, channel="output")
    ws.accept.assert_called_once()
    assert registry.get_output_client("client-1") is ws


@pytest.mark.asyncio
async def test_connection_registry_connect_pipecat():
    from app.core.connection import ConnectionRegistry
    registry = ConnectionRegistry()
    ws = AsyncMock()
    ws.accept = AsyncMock()
    await registry.connect(ws, "pipecat-1", is_pipecat=True)
    assert registry.get_pipecat("pipecat-1") is ws


@pytest.mark.asyncio
async def test_connection_registry_disconnect():
    from app.core.connection import ConnectionRegistry
    registry = ConnectionRegistry()
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    await registry.connect(ws, "client-2", is_pipecat=False, channel="output")
    await registry.disconnect("client-2", is_pipecat=False, channel="output")
    assert registry.get_client("client-2") is None


@pytest.mark.asyncio
async def test_connection_registry_separates_input_and_output():
    from app.core.connection import ConnectionRegistry
    registry = ConnectionRegistry()
    ws_in = AsyncMock()
    ws_in.accept = AsyncMock()
    ws_out = AsyncMock()
    ws_out.accept = AsyncMock()

    await registry.connect(ws_in, "client-3", channel="input")
    await registry.connect(ws_out, "client-3", channel="output")

    assert registry.get_input_client("client-3") is ws_in
    assert registry.get_output_client("client-3") is ws_out
    assert registry.get_clients("client-3") == [ws_in, ws_out]
