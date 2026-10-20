import unittest
from unittest import mock

from scripts.meetingbaas_api import create_meeting_bot


def _created(bot_id: str = "bot-123") -> mock.Mock:
    response = mock.Mock()
    response.status_code = 201
    response.json.return_value = {"success": True, "data": {"bot_id": bot_id}}
    return response


class CreateMeetingBotPayloadTest(unittest.TestCase):
    def _payload(self, **overrides) -> dict:
        kwargs = dict(
            meeting_url="https://meet.google.com/abc-defg-hij",
            websocket_url="wss://bots.example.com",
            bot_id="client-1",
            bot_name="Vibe",
            api_key="key",
        )
        kwargs.update(overrides)
        with mock.patch("scripts.meetingbaas_api.requests.post", return_value=_created()) as post:
            self.assertEqual(create_meeting_bot(**kwargs), "bot-123")
        return post.call_args.kwargs["json"]

    def test_sends_the_display_name_and_asks_for_a_transcript(self) -> None:
        payload = self._payload()

        self.assertEqual(payload["bot_name"], "Vibe")
        self.assertTrue(payload["transcription_enabled"])
        self.assertEqual(payload["transcription_config"], {"provider": "gladia"})
        self.assertTrue(payload["streaming_enabled"])
        self.assertEqual(
            payload["streaming_config"]["input_url"], "wss://bots.example.com/ws/client-1"
        )

    def test_honours_the_chosen_provider(self) -> None:
        payload = self._payload(transcription_provider="deepgram")

        self.assertEqual(payload["transcription_config"], {"provider": "deepgram"})

    def test_can_skip_the_transcript(self) -> None:
        payload = self._payload(transcription_enabled=False)

        self.assertFalse(payload["transcription_enabled"])
        self.assertNotIn("transcription_config", payload)


if __name__ == "__main__":
    unittest.main()
