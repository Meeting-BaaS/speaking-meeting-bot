import unittest

from utils.meeting_memory import MAX_PENDING_CHARS, MeetingMemory


class MeetingMemoryTest(unittest.TestCase):
    def test_wake_text_carries_what_was_said_since_the_bot_last_spoke(self):
        memory = MeetingMemory()
        memory.add("Alice: we should go with option B", at=1)
        memory.add("Bob: fine, but not above five hundred", at=2)
        text = memory.wake_text("Amr: Atlas, what did we agree?")
        self.assertIn("Said in the meeting since you last spoke", text)
        self.assertIn("Alice: we should go with option B", text)
        self.assertIn("Bob: fine, but not above five hundred", text)
        self.assertTrue(text.endswith("Now said to you:\nAmr: Atlas, what did we agree?"))
        # Delivered once: the next wake carries only what came after.
        memory.add("Atlas: You agreed on option B.", kind="bot", at=3)
        memory.mark_delivered()
        memory.add("Carol: and the deadline moves to Friday", at=4)
        again = memory.wake_text("Amr: Atlas, anything else?")
        self.assertNotIn("option B", again.split("Now said to you")[0])
        self.assertIn("Carol: and the deadline moves to Friday", again)

    def test_wake_text_without_pending_is_just_the_sentence(self):
        memory = MeetingMemory()
        self.assertEqual(memory.wake_text("Atlas, hello"), "Atlas, hello")

    def test_pending_is_bounded_and_keeps_the_latest(self):
        memory = MeetingMemory()
        for n in range(200):
            memory.add(f"Alice: sentence number {n} " + "x" * 100, at=n)
        pending = memory.pending()
        self.assertLessEqual(sum(len(e.text) + 1 for e in pending), MAX_PENDING_CHARS)
        self.assertTrue(pending[-1].text.startswith("Alice: sentence number 199"))

    def test_records_keep_bot_and_human_turns_in_order(self):
        memory = MeetingMemory()
        memory.add("Alice: hi", at=1)
        memory.add("Atlas: hello", kind="bot", at=2)
        memory.add("   ", at=3)
        records = memory.as_records()
        self.assertEqual([r["kind"] for r in records], ["human", "bot"])
        self.assertEqual(records[1]["text"], "Atlas: hello")
