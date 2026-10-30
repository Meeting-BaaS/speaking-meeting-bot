import asyncio
import os
import subprocess
import sys
import unittest

from utils.early_audio import hold_messages_until_started


class FakeInputTransport:
    def __init__(self):
        self.delivered = []

    async def on_message(self, websocket, message):
        self.delivered.append(message)


class HoldMessagesTest(unittest.TestCase):
    def test_messages_before_start_are_held_then_delivered_in_order(self):
        transport = FakeInputTransport()
        hold_messages_until_started(transport)

        async def run():
            await transport.on_message(None, b"a")
            await transport.on_message(None, b"b")
            self.assertEqual(transport.delivered, [])
            transport._audio_in_queue = asyncio.Queue()  # what start() does
            await transport.on_message(None, b"c")
            self.assertEqual(transport.delivered, [b"a", b"b", b"c"])
            await transport.on_message(None, b"d")
            self.assertEqual(transport.delivered, [b"a", b"b", b"c", b"d"])

        asyncio.run(run())


class RealTransportTest(unittest.TestCase):
    def test_pipecat_transport_survives_audio_before_start(self):
        # Reproduces the preprod failure against pipecat's own transport: an
        # audio message before start() raised AttributeError and ended the
        # receive loop. With the guard the message is held, then queued once
        # the transport starts. Runs in a subprocess because importing pipecat's
        # protobufs collides with the API's registration in-process.
        code = (
            "import asyncio\n"
            "from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame\n"
            "from pipecat.serializers.protobuf import ProtobufFrameSerializer\n"
            "from pipecat.transports.websocket.client import WebsocketClientParams, WebsocketClientTransport\n"
            "from utils.early_audio import hold_messages_until_started\n"
            "async def run():\n"
            "    s = ProtobufFrameSerializer()\n"
            "    msg = await s.serialize(OutputAudioRawFrame(audio=b'\\x00' * 320, sample_rate=16000, num_channels=1))\n"
            "    assert isinstance(await s.deserialize(msg), InputAudioRawFrame)\n"
            "    def make():\n"
            "        t = WebsocketClientTransport(uri='ws://127.0.0.1:1/x', params=WebsocketClientParams(audio_in_enabled=True, serializer=ProtobufFrameSerializer()))\n"
            "        return t.input()\n"
            "    bare = make()\n"
            "    try:\n"
            "        await bare.on_message(None, msg)\n"
            "        raise SystemExit('expected AttributeError before start on an unguarded transport')\n"
            "    except AttributeError:\n"
            "        pass\n"
            "    guarded = make()\n"
            "    hold_messages_until_started(guarded)\n"
            "    await guarded.on_message(None, msg)\n"
            "    guarded._audio_in_queue = asyncio.Queue()\n"
            "    await guarded.on_message(None, msg)\n"
            "    print(guarded._audio_in_queue.qsize())\n"
            "asyncio.run(run())\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True, capture_output=True, text=True, env=os.environ.copy(), timeout=120,
        )
        self.assertEqual(result.stdout.strip().splitlines()[-1], "2", result.stderr[-800:])
