import os
import unittest

from app.models import BotCredentials, BotRequest
from utils.credentials import credential_env_overrides
from utils.llm_config import missing_llm_provider_credential


class BotCredentialsModelTest(unittest.TestCase):
    def test_keys_are_masked_in_repr_and_dump(self) -> None:
        creds = BotCredentials(llm_api_key="sk-secret", tts_api_key="cart-secret")

        self.assertNotIn("sk-secret", repr(creds))
        self.assertNotIn("sk-secret", str(creds.model_dump()))
        self.assertEqual(
            creds.reveal(), {"llm_api_key": "sk-secret", "tts_api_key": "cart-secret"}
        )

    def test_blank_keys_are_dropped_on_reveal(self) -> None:
        creds = BotCredentials(llm_api_key="   ", stt_api_key="dg-secret")

        self.assertEqual(creds.reveal(), {"stt_api_key": "dg-secret"})

    def test_unknown_credential_fields_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BotCredentials(openai_api_key="sk-secret")

    def test_request_accepts_credentials_and_marks_them_write_only(self) -> None:
        request = BotRequest(
            meeting_url="https://meet.google.com/abc-defg-hij",
            credentials={"llm_api_key": "sk-secret"},
        )
        self.assertEqual(request.credentials.reveal(), {"llm_api_key": "sk-secret"})

        schema = BotCredentials.model_json_schema()
        for field in ("llm_api_key", "stt_api_key", "tts_api_key"):
            options = schema["properties"][field]
            self.assertTrue(options.get("writeOnly"), field)
            self.assertEqual(options.get("format"), "password", field)


class CredentialEnvOverridesTest(unittest.TestCase):
    def test_llm_key_maps_to_the_selected_provider_env(self) -> None:
        self.assertEqual(
            credential_env_overrides("anthropic", {"llm_api_key": "a-key"}),
            {"ANTHROPIC_API_KEY": "a-key"},
        )
        self.assertEqual(
            credential_env_overrides("zai", {"llm_api_key": "z-key"}),
            {"ZAI_API_KEY": "z-key"},
        )

    def test_stt_and_tts_keys_map_to_runner_envs(self) -> None:
        self.assertEqual(
            credential_env_overrides(
                "openai",
                {"llm_api_key": "o-key", "stt_api_key": "dg", "tts_api_key": "ct"},
            ),
            {"OPENAI_API_KEY": "o-key", "DEEPGRAM_API_KEY": "dg", "CARTESIA_API_KEY": "ct"},
        )

    def test_blank_values_never_override_server_keys(self) -> None:
        self.assertEqual(
            credential_env_overrides("openai", {"llm_api_key": "", "tts_api_key": " "}),
            {},
        )
        self.assertEqual(credential_env_overrides("openai", None), {})

    def test_unsupported_provider_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            credential_env_overrides("cohere", {"llm_api_key": "x"})


class MissingCredentialWithRequestKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original = os.environ.pop("OPENAI_API_KEY", None)

    def tearDown(self) -> None:
        if self.original is not None:
            os.environ["OPENAI_API_KEY"] = self.original

    def test_request_key_satisfies_the_check_without_env(self) -> None:
        self.assertEqual(missing_llm_provider_credential("openai"), "OPENAI_API_KEY")
        self.assertIsNone(missing_llm_provider_credential("openai", "sk-from-request"))
        self.assertEqual(
            missing_llm_provider_credential("openai", "  "), "OPENAI_API_KEY"
        )
