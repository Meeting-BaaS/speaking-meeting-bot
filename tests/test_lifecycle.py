import asyncio
import os
import signal
import subprocess
import sys
import unittest
import uuid

from fastapi.testclient import TestClient

from app.main import create_app
from core.connection import BOT_CREDENTIALS, MEETING_DETAILS, PIPECAT_PROCESSES, registry
from core.lifecycle import ShutdownReason, shutdown_bot
from core.router import router as message_router


class FakeSocket:
    def __init__(self):
        self.closed = False

    async def close(self, code=1000, reason=""):
        self.closed = True


class FakeProcess:
    """poll() turns 0 once the child is told to go, by socket or by signal."""

    def __init__(self, exits_on_socket_close: bool):
        self._exits_on_socket_close = exits_on_socket_close
        self.terminated = False
        self.killed = False
        self.pid = 4242

    def poll(self):
        if self.terminated or (self._exits_on_socket_close and not registry.pipecat_connections):
            return 0
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def register_bot(client_id: str, bot_id: str, process: FakeProcess) -> None:
    MEETING_DETAILS[client_id] = (
        "https://meet.google.com/abc-defg-hij",
        "Vibe",
        bot_id,
        False,
        "16khz",
        {"name": "Vibe", "prompt": "You are Vibe."},
        [],
    )
    BOT_CREDENTIALS[client_id] = {"llm_api_key": "sk-test"}
    PIPECAT_PROCESSES[client_id] = process
    registry.pipecat_connections[client_id] = FakeSocket()
    registry.active_connections[client_id] = FakeSocket()


class ShutdownBotTest(unittest.TestCase):
    def tearDown(self):
        message_router.closing_clients.clear()

    def test_child_that_exits_on_socket_close_is_not_signalled(self):
        client_id, bot_id = str(uuid.uuid4()), str(uuid.uuid4())
        process = FakeProcess(exits_on_socket_close=True)
        register_bot(client_id, bot_id, process)

        outcome = asyncio.run(shutdown_bot(client_id, reason="test", exit_grace_secs=1.0))

        self.assertEqual(outcome["process"], "exited")
        self.assertEqual(outcome["sockets_closed"], 2)
        self.assertFalse(process.terminated)
        self.assertNotIn(client_id, MEETING_DETAILS)
        self.assertNotIn(client_id, BOT_CREDENTIALS)
        self.assertNotIn(client_id, PIPECAT_PROCESSES)
        self.assertNotIn(client_id, registry.pipecat_connections)
        self.assertIn(client_id, message_router.closing_clients)

    def test_child_that_lingers_gets_sigterm(self):
        client_id, bot_id = str(uuid.uuid4()), str(uuid.uuid4())
        process = FakeProcess(exits_on_socket_close=False)
        register_bot(client_id, bot_id, process)

        outcome = asyncio.run(shutdown_bot(client_id, reason="test", exit_grace_secs=0.2))

        self.assertEqual(outcome["process"], "terminated")
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertNotIn(client_id, MEETING_DETAILS)

    def test_unknown_bot_is_a_no_op(self):
        outcome = asyncio.run(shutdown_bot(str(uuid.uuid4()), reason="test"))
        self.assertEqual(outcome["process"], "none")
        self.assertEqual(outcome["sockets_closed"], 0)


class ShutdownReasonTest(unittest.TestCase):
    def test_reads_as_a_sentence_fragment(self):
        self.assertEqual(
            ShutdownReason.with_detail(ShutdownReason.CALL_ENDED, "bot.completed"),
            "call ended (bot.completed)",
        )
        self.assertEqual(
            ShutdownReason.with_detail(ShutdownReason.REMOVED_BY_CALLER, None),
            "removed by caller",
        )


class EndOfCallWebhookTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())

    def tearDown(self):
        message_router.closing_clients.clear()

    def _end_event(self, body: dict) -> tuple[str, FakeProcess]:
        client_id, bot_id = str(uuid.uuid4()), str(uuid.uuid4())
        process = FakeProcess(exits_on_socket_close=True)
        register_bot(client_id, bot_id, process)
        body.setdefault("data", {})["bot_id"] = bot_id
        response = self.client.post("/webhook", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return client_id, process

    def test_bot_completed_tears_the_bot_down(self):
        client_id, process = self._end_event({"event": "bot.completed"})
        self.assertNotIn(client_id, MEETING_DETAILS)
        self.assertNotIn(client_id, PIPECAT_PROCESSES)
        self.assertEqual(process.poll(), 0)

    def test_bot_failed_tears_the_bot_down(self):
        client_id, _ = self._end_event({"event": "bot.failed"})
        self.assertNotIn(client_id, MEETING_DETAILS)
        self.assertNotIn(client_id, PIPECAT_PROCESSES)

    def test_status_change_into_the_call_keeps_the_bot(self):
        client_id, _ = self._end_event(
            {"event": "bot.status_change", "data": {"status": {"code": "in_call_recording"}}}
        )
        self.assertIn(client_id, MEETING_DETAILS)
        self.assertIn(client_id, PIPECAT_PROCESSES)
        asyncio.run(shutdown_bot(client_id, reason="cleanup", exit_grace_secs=0.1))

    def test_unknown_bot_is_ignored(self):
        response = self.client.post(
            "/webhook", json={"event": "bot.completed", "data": {"bot_id": str(uuid.uuid4())}}
        )
        self.assertEqual(response.json(), {"status": "ignored"})


class RunnerSigtermTest(unittest.TestCase):
    def test_sigterm_ends_the_pipeline_and_runs_cleanup(self):
        # The API's last resort for a child that outstayed its socket is
        # SIGTERM; with the runner handling it the pipeline is cancelled and
        # the code after runner.run() still executes.
        code = (
            "import asyncio, sys\n"
            "from scripts import meetingbaas as m\n"
            "async def announce():\n"
            "    # runner.run() installs its signal handlers as it starts; by the\n"
            "    # time this fires it has been running for a second.\n"
            "    await asyncio.sleep(1.0)\n"
            "    print('running', flush=True)\n"
            "async def check():\n"
            "    worker, runner = await m.build_pipeline_runner(m.Pipeline([]))\n"
            "    asyncio.get_running_loop().create_task(announce())\n"
            "    try:\n"
            "        await runner.run()\n"
            "    finally:\n"
            "        print('cleanup ran', flush=True)\n"
            "asyncio.run(check())\n"
        )
        with subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
        ) as proc:
            for line in proc.stdout:
                if line.strip() == "running":
                    break
            proc.send_signal(signal.SIGTERM)
            rest = proc.stdout.read()
            proc.wait(timeout=30)
        self.assertIn("cleanup ran", rest)
        self.assertEqual(proc.returncode, 0, rest)
