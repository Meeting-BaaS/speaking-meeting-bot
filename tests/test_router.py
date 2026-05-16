"""
Testing Agent — MessageRouter unit tests.
Covers: send_binary, send_text, broadcast, mark_closing, send_to_pipecat, send_from_pipecat.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get_clients = MagicMock(return_value=[])
    registry.iter_unique_clients = MagicMock(return_value=[])
    return registry


@pytest.fixture
def mock_converter():
    converter = MagicMock()
    converter.raw_to_protobuf = AsyncMock(return_value=b"protobuf_frame")
    converter.protobuf_to_raw = AsyncMock(return_value=(b"raw_audio", None))
    return converter


@pytest.fixture
def router(mock_registry, mock_converter):
    from app.core.router import MessageRouter
    return MessageRouter(mock_registry, mock_converter)


# ─────────────────────────────────────────────────────────────────────────────
# mark_closing
# ─────────────────────────────────────────────────────────────────────────────

def test_mark_closing(router):
    router.mark_closing("client-1")
    assert "client-1" in router.closing_clients


# ─────────────────────────────────────────────────────────────────────────────
# send_binary
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_binary_skips_closing(router):
    router.mark_closing("client-1")
    mock_ws = AsyncMock()
    router.registry.get_output_client = MagicMock(return_value=mock_ws)
    await router.send_binary(b"data", "client-1")
    mock_ws.send_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_send_binary_sends_to_client(router):
    mock_ws = AsyncMock()
    router.registry.get_output_client = MagicMock(return_value=mock_ws)
    await router.send_binary(b"audio_data", "client-2")
    mock_ws.send_bytes.assert_called_once_with(b"audio_data")


@pytest.mark.asyncio
async def test_send_binary_no_client(router):
    router.registry.get_output_client = MagicMock(return_value=None)
    # Should not raise, just silently skip
    await router.send_binary(b"data", "no-client")


# ─────────────────────────────────────────────────────────────────────────────
# send_text
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_text_sends_to_client(router):
    mock_ws = AsyncMock()
    router.registry.get_clients = MagicMock(return_value=[mock_ws])
    await router.send_text("hello", "client-3")
    mock_ws.send_text.assert_called_once_with("hello")


@pytest.mark.asyncio
async def test_send_text_skips_closing(router):
    router.mark_closing("client-4")
    mock_ws = AsyncMock()
    router.registry.get_clients = MagicMock(return_value=[mock_ws])
    await router.send_text("hello", "client-4")
    mock_ws.send_text.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# broadcast
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_broadcast(router):
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    router.registry.iter_unique_clients = MagicMock(return_value=[("c1", ws1), ("c2", ws2)])
    await router.broadcast("system message")
    ws1.send_text.assert_called_once_with("system message")
    ws2.send_text.assert_called_once_with("system message")


@pytest.mark.asyncio
async def test_broadcast_skips_closing(router):
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    router.registry.iter_unique_clients = MagicMock(return_value=[("c1", ws1), ("c2", ws2)])
    router.mark_closing("c1")
    await router.broadcast("msg")
    ws1.send_text.assert_not_called()
    ws2.send_text.assert_called_once_with("msg")


# ─────────────────────────────────────────────────────────────────────────────
# send_to_pipecat
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_to_pipecat_converts_and_sends(router, mock_converter):
    mock_pipecat_ws = AsyncMock()
    router.registry.get_pipecat = MagicMock(return_value=mock_pipecat_ws)
    await router.send_to_pipecat(b"raw_audio_bytes", "client-5")
    mock_converter.raw_to_protobuf.assert_called_once_with(b"raw_audio_bytes")
    mock_pipecat_ws.send_bytes.assert_called_once_with(b"protobuf_frame")


@pytest.mark.asyncio
async def test_send_to_pipecat_skips_closing(router):
    router.mark_closing("client-6")
    mock_pipecat_ws = AsyncMock()
    router.registry.get_pipecat = MagicMock(return_value=mock_pipecat_ws)
    await router.send_to_pipecat(b"data", "client-6")
    mock_pipecat_ws.send_bytes.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# send_from_pipecat
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_from_pipecat_converts_and_sends(router, mock_converter):
    mock_client_ws = AsyncMock()
    router.registry.get_output_client = MagicMock(return_value=mock_client_ws)
    await router.send_from_pipecat(b"proto_frame", "client-7")
    mock_converter.protobuf_to_raw.assert_called_once_with(b"proto_frame")
    mock_client_ws.send_bytes.assert_called_once_with(b"raw_audio")


@pytest.mark.asyncio
async def test_send_from_pipecat_skips_closing(router):
    router.mark_closing("client-8")
    mock_ws = AsyncMock()
    router.registry.get_output_client = MagicMock(return_value=mock_ws)
    await router.send_from_pipecat(b"data", "client-8")
    mock_ws.send_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_send_from_pipecat_none_audio_falls_back(router, mock_converter):
    """When protobuf_to_raw returns None, fall back to forwarding raw bytes if large enough."""
    mock_converter.protobuf_to_raw = AsyncMock(return_value=(None, None))
    mock_ws = AsyncMock()
    router.registry.get_output_client = MagicMock(return_value=mock_ws)
    large_payload = b"x" * 200
    await router.send_from_pipecat(large_payload, "client-9")
    mock_ws.send_bytes.assert_called_once_with(large_payload)
