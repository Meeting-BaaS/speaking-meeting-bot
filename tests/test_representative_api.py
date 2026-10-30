"""The representative surface of the API: authority, callbacks, owner messages, usage, briefing."""

import json
import os
import tempfile
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from app.main import create_app
from core.connection import BOT_CREDENTIALS, MEETING_DETAILS, PIPECAT_PROCESSES, registry
from core.router import router as message_router
from utils.authority import new_escalation, read_escalations, record_escalation
from utils.usage_meter import UsageLedger, usage_dir

HEADERS = {"x-meeting-baas-api-key": "mb-key"}
OTHER_HEADERS = {"x-meeting-baas-api-key": "someone-elses-key"}
MEET = "https://meet.google.com/abc-defg-hij"


class FakeProcess:
    pid = 1

    def poll(self):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def _forget(client_id):
    MEETING_DETAILS.pop(client_id, None)
    BOT_CREDENTIALS.pop(client_id, None)
    PIPECAT_PROCESSES.pop(client_id, None)


class RepresentativeApiTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())
        self.state_dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ,
            {"SPEAKING_BOT_STATE_DIR": self.state_dir.name, "OPENAI_API_KEY": "sk-server", "LLM_PROVIDER": "openai"},
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.state_dir.cleanup()
        message_router.closing_clients.clear()

    def _create(self, **extra):
        bot_id = str(uuid.uuid4())
        with (
            mock.patch("app.routes.create_meeting_bot", return_value=bot_id),
            mock.patch("app.routes.start_pipecat_process", return_value=FakeProcess()),
            mock.patch("app.routes._poll_bot_admission", new=mock.AsyncMock()),
            mock.patch("app.routes.image_service.generate_persona_image", new=mock.AsyncMock(return_value=None)),
            mock.patch("app.routes.extract_persona_details_from_prompt", new=mock.AsyncMock(return_value={})),
        ):
            response = self.client.post(
                "/bots",
                headers=HEADERS,
                json={"meeting_url": MEET, "bot_name": "RepMe", "prompt": "Attend for Ada.", **extra},
            )
        self.assertEqual(response.status_code, 201, response.text)
        client_id = next(cid for cid, d in MEETING_DETAILS.items() if d[2] == bot_id)
        return bot_id, client_id

    def test_request_fields_reach_the_child_and_the_secret_stays_in_memory(self):
        bot_id, client_id = self._create(
            language="en-NG",
            callback_url="https://app.example.com/hook",
            callback_secret="shh",
            authority={"owner_name": "Ada", "escalate_on": ["spending"]},
            briefing={"language": "ha"},
            credentials={"tts_api_key": "cartesia-own"},
        )
        try:
            persona = MEETING_DETAILS[client_id][5]
            self.assertEqual(persona["language"], "en-NG")
            self.assertEqual(persona["callback_url"], "https://app.example.com/hook")
            self.assertEqual(persona["authority"]["owner_name"], "Ada")
            self.assertEqual(persona["briefing"], {"enabled": True, "language": "ha"})
            self.assertEqual(persona["display_name"], "RepMe")
            self.assertEqual(persona["key_sources"], {"llm": "platform", "stt": "platform", "tts": "caller"})
            self.assertNotIn("callback_secret", json.dumps(persona))
            self.assertEqual(BOT_CREDENTIALS[client_id], {"tts_api_key": "cartesia-own", "callback_secret": "shh"})
        finally:
            _forget(client_id)

    def test_owner_messages_and_escalation_answers_are_queued_for_the_bot(self):
        bot_id, client_id = self._create(authority={"owner_name": "Ada"})
        try:
            self.assertEqual(self.client.post(f"/bots/{uuid.uuid4()}/messages", headers=HEADERS, json={"text": "x"}).status_code, 404)
            queued = self.client.post(f"/bots/{bot_id}/messages", headers=HEADERS, json={"text": "Tell them I am late"})
            self.assertEqual(queued.status_code, 202, queued.text)
            self.assertTrue(queued.json()["message_id"])

            self.assertEqual(self.client.get(f"/bots/{bot_id}/escalations", headers=HEADERS).json(), [])
            missing = self.client.post(
                f"/bots/{bot_id}/escalations/{uuid.uuid4()}/response", headers=HEADERS, json={"response": "No"}
            )
            self.assertEqual(missing.status_code, 404)

            escalation = new_escalation(question="Spend 600?", category="spending", context=None)
            record_escalation(self.state_dir.name, client_id, escalation)
            listed = self.client.get(f"/bots/{bot_id}/escalations", headers=HEADERS).json()
            self.assertEqual(listed[0]["question"], "Spend 600?")
            answered = self.client.post(
                f"/bots/{bot_id}/escalations/{escalation['escalation_id']}/response",
                headers=HEADERS,
                json={"response": "No, hold at 500", "decision": "declined"},
            )
            self.assertEqual(answered.status_code, 202, answered.text)
            self.assertEqual(read_escalations(self.state_dir.name, client_id)[0]["status"], "answered")

            with open(os.path.join(self.state_dir.name, "owner_messages", f"{client_id}.jsonl")) as handle:
                lines = [json.loads(line) for line in handle]
            self.assertEqual([line["type"] for line in lines], ["owner_message", "escalation_response"])
        finally:
            _forget(client_id)

    def test_usage_is_readable_live_and_after_the_call(self):
        bot_id, client_id = self._create()
        try:
            self.assertEqual(self.client.get(f"/bots/{bot_id}/usage", headers=HEADERS).status_code, 404)
            ledger = UsageLedger(bot_id=client_id, tts_characters=120, llm_turns=2)
            ledger.save(usage_dir(self.state_dir.name))
            live = self.client.get(f"/bots/{bot_id}/usage", headers=HEADERS)
            self.assertEqual(live.status_code, 200, live.text)
            self.assertEqual(live.json()["tts_characters"], 120)
        finally:
            _forget(client_id)

    def test_call_end_writes_the_briefing_files_usage_and_posts_the_ended_event(self):
        bot_id, client_id = self._create(
            callback_url="https://app.example.com/hook",
            callback_secret="shh",
            authority={"owner_name": "Ada"},
            briefing={"language": "yo"},
        )
        PIPECAT_PROCESSES[client_id] = FakeProcess()
        transcripts = os.path.join(self.state_dir.name, "transcripts")
        os.makedirs(transcripts, exist_ok=True)
        with open(os.path.join(transcripts, f"{client_id}.json"), "w") as handle:
            json.dump(
                {"bot_id": client_id, "persona_name": "RepMe", "messages": [
                    {"role": "user", "content": "Alice: can we spend 600?"},
                    {"role": "assistant", "content": "I will check with Ada."},
                ]},
                handle,
            )
        UsageLedger(bot_id=client_id, tts_characters=33).save(usage_dir(self.state_dir.name))
        record_escalation(self.state_dir.name, client_id, new_escalation(question="Spend 600?", category="spending", context=None))

        seen = {}

        def fake_generate(**kwargs):
            seen.update(kwargs)
            return {"summary": "Alice wants 600.", "unresolved": ["Spend 600?"], "language": "yo", "generated_at": 1.0}

        sent = []

        async def fake_send(self, event, data):
            sent.append((self.url, self.secret, self.meetingbaas_bot_id, event, data))
            return True

        with (
            mock.patch("app.routes.generate_briefing", side_effect=fake_generate),
            mock.patch("app.routes.CustomerEventSink.send", new=fake_send),
        ):
            response = self.client.post("/webhook", json={"event": "bot.completed", "data": {"bot_id": bot_id}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(client_id, MEETING_DETAILS)

        self.assertEqual(seen["api_key"], "sk-server")
        self.assertEqual(seen["bot_name"], "RepMe")
        self.assertEqual(seen["persona"]["briefing"]["language"], "yo")
        self.assertEqual(seen["escalations"][0]["question"], "Spend 600?")
        self.assertEqual(seen["coverage"]["open_escalations"], 1)

        briefing = self.client.get(f"/bots/{bot_id}/briefing", headers=HEADERS)
        self.assertEqual(briefing.status_code, 200, briefing.text)
        self.assertEqual(briefing.json()["summary"], "Alice wants 600.")
        usage = self.client.get(f"/bots/{bot_id}/usage", headers=HEADERS).json()
        self.assertEqual(usage["meetingbaas_bot_id"], bot_id)
        self.assertEqual(usage["tts_characters"], 33)
        self.assertIsNotNone(usage["ended_at"])

        self.assertEqual(len(sent), 1)
        url, secret, mb_id, event, data = sent[0]
        self.assertEqual((url, secret, mb_id, event), ("https://app.example.com/hook", "shh", bot_id, "speaking_bot.ended"))
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["briefing"]["summary"], "Alice wants 600.")
        self.assertEqual(data["usage"]["tts_characters"], 33)
        self.assertEqual(data["escalations"][0]["status"], "pending")

    def test_only_the_creating_key_can_drive_or_read_a_bot(self):
        bot_id, client_id = self._create(authority={"owner_name": "Ada"}, briefing={})
        PIPECAT_PROCESSES[client_id] = FakeProcess()
        UsageLedger(bot_id=client_id, tts_characters=5).save(usage_dir(self.state_dir.name))
        escalation = new_escalation(question="Spend?", category="spending", context=None)
        record_escalation(self.state_dir.name, client_id, escalation)
        live_calls = (
            ("post", f"/bots/{bot_id}/messages", {"text": "hi"}),
            ("get", f"/bots/{bot_id}/escalations", None),
            ("post", f"/bots/{bot_id}/escalations/{escalation['escalation_id']}/response", {"response": "no"}),
            ("get", f"/bots/{bot_id}/usage", None),
        )
        for method, path, body in live_calls:
            other = self.client.request(method.upper(), path, headers=OTHER_HEADERS, json=body)
            self.assertEqual(other.status_code, 404, (path, other.text))
            mine = self.client.request(method.upper(), path, headers=HEADERS, json=body)
            self.assertIn(mine.status_code, (200, 202), (path, mine.text))

        transcripts = os.path.join(self.state_dir.name, "transcripts")
        os.makedirs(transcripts, exist_ok=True)
        with open(os.path.join(transcripts, f"{client_id}.json"), "w") as handle:
            json.dump({"bot_id": client_id, "persona_name": "RepMe", "messages": [
                {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]}, handle)
        with mock.patch("app.routes.generate_briefing", return_value={"summary": "s", "generated_at": 1.0}):
            self.client.post("/webhook", json={"event": "bot.completed", "data": {"bot_id": bot_id}})
        for path in (f"/bots/{bot_id}/usage", f"/bots/{bot_id}/briefing"):
            self.assertEqual(self.client.get(path, headers=OTHER_HEADERS).status_code, 404, path)
            mine = self.client.get(path, headers=HEADERS)
            self.assertEqual(mine.status_code, 200, (path, mine.text))
            self.assertNotIn("owner_key_digest", mine.json())

    def test_callback_alone_does_not_trigger_a_briefing(self):
        bot_id, client_id = self._create(callback_url="https://app.example.com/hook")
        PIPECAT_PROCESSES[client_id] = FakeProcess()
        transcripts = os.path.join(self.state_dir.name, "transcripts")
        os.makedirs(transcripts, exist_ok=True)
        with open(os.path.join(transcripts, f"{client_id}.json"), "w") as handle:
            json.dump({"bot_id": client_id, "persona_name": "RepMe", "messages": [
                {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]}, handle)
        sent = []

        async def fake_send(self, event, data):
            sent.append(data)
            return True

        # The legacy call summary (pre-existing, server OpenAI key) is untouched
        # here; only the briefing must stay off when it was not asked for.
        with (
            mock.patch("app.routes.generate_briefing") as briefing,
            mock.patch("app.routes._generate_summary_sync"),
            mock.patch("app.routes.CustomerEventSink.send", new=fake_send),
        ):
            self.client.post("/webhook", json={"event": "bot.completed", "data": {"bot_id": bot_id}})
        briefing.assert_not_called()
        self.assertEqual(len(sent), 1)
        self.assertIsNone(sent[0]["briefing"])

    def test_call_end_without_a_representative_keeps_the_legacy_summary_path(self):
        bot_id, client_id = self._create()
        PIPECAT_PROCESSES[client_id] = FakeProcess()
        with (
            mock.patch("app.routes.generate_briefing") as briefing,
            mock.patch("app.routes._generate_summary_sync") as legacy,
        ):
            self.client.post("/webhook", json={"event": "bot.completed", "data": {"bot_id": bot_id}})
        briefing.assert_not_called()
        legacy.assert_not_called()  # no transcript file in this state dir
        self.assertEqual(self.client.get(f"/bots/{bot_id}/briefing", headers=HEADERS).status_code, 404)

    def test_rejected_fields(self):
        for bad in (
            {"language": "English"},
            {"callback_url": "http://public.example.com/hook"},
            {"callback_url": "https://127.0.0.1/hook"},
            {"callback_url": "https://localhost/hook"},
            {"callback_url": "https://api.internal/hook"},
            {"authority": {"owner_name": "Ada", "extra": 1}},
            {"briefing": {"language": "x-"}},
        ):
            response = self.client.post("/bots", headers=HEADERS, json={"meeting_url": MEET, "prompt": "x", **bad})
            self.assertEqual(response.status_code, 422, bad)
        self.assertEqual(registry.active_connections, {})
