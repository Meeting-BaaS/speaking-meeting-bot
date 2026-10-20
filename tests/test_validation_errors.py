import unittest

from fastapi.testclient import TestClient

from app.main import create_app


class ValidationErrorsDoNotEchoSecretsTest(unittest.TestCase):
    def test_422_omits_the_offending_input(self) -> None:
        client = TestClient(create_app())

        response = client.post(
            "/bots",
            headers={"x-meeting-baas-api-key": "mb-key"},
            json={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "credentials": {"llm_api_key": "sk-very-secret", "bogus": "x"},
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn("sk-very-secret", response.text)
        self.assertEqual(response.json()["detail"][0]["type"], "extra_forbidden")
