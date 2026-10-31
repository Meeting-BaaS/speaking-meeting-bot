import json
import tempfile
import unittest
from unittest import mock

from utils.briefing import (
    BRIEFING_KEYS,
    build_briefing_messages,
    generate_briefing,
    load_briefing,
    parse_briefing,
    save_briefing,
    transcript_text,
)


class BriefingPromptTest(unittest.TestCase):
    def test_transcript_keeps_speaker_labels_and_marks_the_bot(self):
        text = transcript_text(
            [
                {"role": "system", "content": "ignored"},
                {"role": "user", "content": "Alice: can we spend 600?"},
                {"role": "assistant", "content": "I need to check with Ada."},
                {"role": "user", "content": "unlabelled remark"},
            ],
            "RepMe",
        )
        self.assertEqual(
            text.splitlines(),
            ["Alice: can we spend 600?", "RepMe (you): I need to check with Ada.", "Participant: unlabelled remark"],
        )

    def test_messages_carry_language_owner_escalations_and_coverage(self):
        system, user = build_briefing_messages(
            bot_name="RepMe",
            owner_name="Ada",
            language="yo",
            instructions="Focus on budget.",
            transcript="Alice: hi",
            escalations=[{"question": "Spend 600?", "status": "answered", "response": "No"}],
            coverage={"bot_failed": False},
        )
        self.assertIn("one JSON object", system)
        self.assertIn("language: yo", user)
        self.assertIn("attending for Ada", user)
        self.assertIn("Focus on budget.", user)
        self.assertIn("[answered] Spend 600? -> answer: No", user)
        self.assertIn("bot_failed: False", user)
        self.assertIn("Alice: hi", user)


class HeardLogTest(unittest.TestCase):
    def test_heard_log_is_the_record_when_present(self):
        text = transcript_text(
            [{"role": "user", "content": "Amr: Atlas, what did we agree?"}],
            "Atlas",
            heard=[
                {"kind": "human", "text": "Alice: we go with option B"},
                {"kind": "human", "text": "unlabelled remark"},
                {"kind": "bot", "text": "Atlas: You agreed on option B."},
            ],
        )
        self.assertEqual(
            text.splitlines(),
            ["Alice: we go with option B", "Participant: unlabelled remark", "Atlas (you): You agreed on option B."],
        )


class BriefingParseTest(unittest.TestCase):
    def test_parses_fenced_json_and_fills_missing_keys(self):
        parsed = parse_briefing('```json\n{"summary": "Short.", "decisions": ["Go B"]}\n```')
        self.assertEqual(parsed["summary"], "Short.")
        self.assertEqual(parsed["decisions"], ["Go B"])
        for key in BRIEFING_KEYS:
            self.assertIn(key, parsed)
        self.assertEqual(parsed["actions"], [])

    def test_prose_becomes_the_summary(self):
        parsed = parse_briefing("The meeting discussed nothing of note.")
        self.assertEqual(parsed["summary"], "The meeting discussed nothing of note.")
        self.assertEqual(parsed["unresolved"], [])


class GenerateBriefingTest(unittest.TestCase):
    def test_runs_on_the_bots_provider_and_language(self):
        calls = []

        def fake_complete(provider, model, api_key, system, user):
            calls.append((provider, model, api_key))
            return json.dumps({"summary": "Done.", "unresolved": ["Budget"]})

        persona = {
            "llm_provider": "anthropic",
            "llm_model": "claude-haiku-4-5",
            "briefing": {"language": "ha"},
            "authority": {"owner_name": "Ada"},
        }
        briefing = generate_briefing(
            persona=persona,
            api_key="sk-test",
            bot_name="RepMe",
            messages=[{"role": "user", "content": "Alice: hello"}],
            complete=fake_complete,
        )
        self.assertEqual(calls, [("anthropic", "claude-haiku-4-5", "sk-test")])
        self.assertEqual(briefing["summary"], "Done.")
        self.assertEqual(briefing["unresolved"], ["Budget"])
        self.assertEqual(briefing["language"], "ha")
        self.assertEqual(briefing["llm_provider"], "anthropic")

        with tempfile.TemporaryDirectory() as state_dir, mock.patch.dict("os.environ", {}, clear=False):
            save_briefing(state_dir, "mb-1", briefing)
            self.assertEqual(load_briefing(state_dir, "nope", "mb-1")["summary"], "Done.")
            self.assertIsNone(load_briefing(state_dir, "nope"))


class LongMeetingTest(unittest.TestCase):
    def test_long_logs_keep_the_opening_and_the_end_and_say_what_is_missing(self):
        from utils.briefing import MAX_TRANSCRIPT_CHARS, bounded_transcript, transcript_lines

        heard = [{"kind": "human", "text": f"Alice: opening decision {n} " + "x" * 200} for n in range(600)]
        lines = transcript_lines([], "Atlas", heard)
        text, omitted = bounded_transcript(lines)
        self.assertGreater(omitted, 0)
        self.assertTrue(text.startswith("Alice: opening decision 0 "))
        self.assertTrue(text.rstrip().endswith("x" * 50))
        self.assertIn(f"[… {omitted} characters from the middle of the meeting are not in this record …]", text)
        self.assertLessEqual(len(text), MAX_TRANSCRIPT_CHARS + 200)

    def test_truncation_is_reported_to_the_briefing_as_coverage(self):
        captured = {}

        def fake_complete(provider, model, api_key, system, user):
            captured["user"] = user
            return json.dumps({"summary": "s"})

        heard = [{"kind": "human", "text": "Alice: " + "y" * 300} for _ in range(400)]
        generate_briefing(persona={}, api_key="k", bot_name="Atlas", messages=[], heard=heard, complete=fake_complete)
        self.assertIn("transcript_truncated", captured["user"])
