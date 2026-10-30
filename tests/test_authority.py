import os
import tempfile
import unittest

from utils.authority import (
    ESCALATE_TOOL_NAME,
    MESSAGE_TYPE_ESCALATION_RESPONSE,
    OwnerMessageReader,
    append_owner_message,
    build_authority_prompt,
    escalation_reply_for_llm,
    escalation_tool_schema,
    new_escalation,
    owner_message_for_llm,
    owner_messages_path,
    read_escalations,
    record_escalation,
)
from utils.system_prompt import build_system_prompt


class AuthorityPromptTest(unittest.TestCase):
    def test_prompt_names_owner_positions_and_escalations_and_ends_the_system_prompt(self):
        authority = {
            "owner_name": "Ada Okafor",
            "approved_positions": ["Ada supports option B"],
            "escalate_on": ["spending above 500 dollars"],
            "forbidden": ["agreeing to new deadlines"],
        }
        block = build_authority_prompt(authority, "RepMe")
        self.assertIn("AI representative attending on behalf of Ada Okafor", block)
        self.assertIn("Ada supports option B", block)
        self.assertIn("spending above 500 dollars", block)
        self.assertIn("agreeing to new deadlines", block)
        self.assertIn(ESCALATE_TOOL_NAME, block)
        self.assertIn("Never approve, commit, promise or agree", block)

        full = build_system_prompt({"prompt": "Be Ada's rep."}, "rep", "", block)
        self.assertTrue(full.endswith(block.strip()))
        self.assertEqual(build_authority_prompt(None, "x"), "")

    def test_without_positions_the_bot_brings_questions_back(self):
        block = build_authority_prompt({"owner_name": "Tunde"}, "Rep")
        self.assertIn("approved no specific positions", block)
        self.assertIn("approving spending or budgets", block)

    def test_tool_schema_and_llm_reply(self):
        schema = escalation_tool_schema()
        self.assertEqual(schema.name, ESCALATE_TOOL_NAME)
        self.assertIn("question", schema.properties)
        self.assertIn("checking", build_authority_prompt({"owner_name": "A"}, "R"))
        self.assertIn("asked Ada", escalation_reply_for_llm("Ada", 2))


class EscalationFilesTest(unittest.TestCase):
    def test_escalations_merge_their_answers(self):
        with tempfile.TemporaryDirectory() as state_dir:
            escalation = new_escalation(question="Approve 600 dollars?", category="spending", context="Vendor asked")
            self.assertEqual(escalation["status"], "pending")
            record_escalation(state_dir, "bot", escalation)
            other = new_escalation(question="Move the deadline?", category="nonsense", context=None)
            self.assertEqual(other["category"], "other")
            record_escalation(state_dir, "bot", other)

            append_owner_message(
                state_dir,
                "bot",
                "No, hold at 500.",
                message_type=MESSAGE_TYPE_ESCALATION_RESPONSE,
                escalation_id=escalation["escalation_id"],
                decision="declined",
            )
            items = read_escalations(state_dir, "bot")
            self.assertEqual([item["question"] for item in items], ["Approve 600 dollars?", "Move the deadline?"])
            self.assertEqual(items[0]["status"], "answered")
            self.assertEqual(items[0]["response"], "No, hold at 500.")
            self.assertEqual(items[0]["decision"], "declined")
            self.assertEqual(items[1]["status"], "pending")
            self.assertEqual(read_escalations(state_dir, "nobody"), [])

    def test_owner_message_reader_returns_only_new_complete_lines(self):
        with tempfile.TemporaryDirectory() as state_dir:
            reader = OwnerMessageReader(owner_messages_path(state_dir, "bot"))
            self.assertEqual(reader.poll(), [])
            first = append_owner_message(state_dir, "bot", "Tell them I am late")
            self.assertEqual([r["message_id"] for r in reader.poll()], [first["message_id"]])
            self.assertEqual(reader.poll(), [])
            path = owner_messages_path(state_dir, "bot")
            with open(path, "a") as handle:
                handle.write('{"message_id": "partial", "type": "owner_message", "text": "half')
            self.assertEqual(reader.poll(), [])
            with open(path, "a") as handle:
                handle.write('"}\n')
            self.assertEqual([r["message_id"] for r in reader.poll()], ["partial"])
            self.assertTrue(os.path.exists(path))

    def test_llm_messages_relay_faithfully(self):
        owner_line = owner_message_for_llm({"type": "owner_message", "text": "Go with B"}, "Ada")
        self.assertIn('"Go with B"', owner_line)
        self.assertIn("Relay this", owner_line)
        answer_line = owner_message_for_llm(
            {"type": MESSAGE_TYPE_ESCALATION_RESPONSE, "text": "Approved", "decision": "approved"}, "Ada"
        )
        self.assertIn("Ada answered your escalation (approved)", answer_line)
        self.assertIn("add nothing", answer_line)
