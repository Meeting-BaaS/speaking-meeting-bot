import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from app.main import create_app
from core.connection import BOT_CREDENTIALS, MEETING_DETAILS, PIPECAT_PROCESSES

NO_KEYS = {
    "LLM_PROVIDER": "anthropic",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "ZAI_API_KEY": "",
    "LOCAL_DEV_MODE": "",
}


class FakeProcess:
    pid = 1

    def poll(self):
        return 0


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in NO_KEYS}
    env.update({k: v for k, v in NO_KEYS.items() if v})
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


class NoServerLlmKeysTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())

    def test_request_without_a_key_is_told_to_bring_one(self):
        with _env():
            response = self.client.post(
                "/bots",
                headers={"x-meeting-baas-api-key": "mb-key"},
                json={"meeting_url": "https://meet.google.com/abc-defg-hij", "prompt": "Be brief."},
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("credentials.llm_api_key", response.json()["message"])
        self.assertIn("ANTHROPIC_API_KEY", response.json()["message"])

    def test_prompt_persona_runs_as_written_with_the_callers_key(self):
        bot_id = str(uuid.uuid4())
        with (
            _env(),
            mock.patch("app.routes.create_meeting_bot", return_value=bot_id),
            mock.patch("app.routes.start_pipecat_process", return_value=FakeProcess()),
            mock.patch("app.routes._poll_bot_admission", new=mock.AsyncMock()),
            mock.patch("app.routes.image_service.generate_persona_image", new=mock.AsyncMock(return_value=None)),
        ):
            response = self.client.post(
                "/bots",
                headers={"x-meeting-baas-api-key": "mb-key"},
                json={
                    "meeting_url": "https://meet.google.com/abc-defg-hij",
                    "bot_name": "Scout",
                    "prompt": "You are Scout, a terse analyst.",
                    "credentials": {"llm_api_key": "sk-ant-callers-own"},
                },
            )
        self.assertEqual(response.status_code, 201, response.text)
        client_id = next(cid for cid, d in MEETING_DETAILS.items() if d[2] == bot_id)
        try:
            persona = MEETING_DETAILS[client_id][5]
            self.assertEqual(persona["name"], "Scout")
            self.assertEqual(persona["prompt"], "You are Scout, a terse analyst.")
            self.assertTrue(persona["is_temporary"])
            self.assertIsNone(persona["cartesia_voice_id"])
            self.assertEqual(BOT_CREDENTIALS[client_id], {"llm_api_key": "sk-ant-callers-own"})
        finally:
            MEETING_DETAILS.pop(client_id, None)
            BOT_CREDENTIALS.pop(client_id, None)
            PIPECAT_PROCESSES.pop(client_id, None)
