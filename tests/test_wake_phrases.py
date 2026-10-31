import unittest

from utils.wake_phrases import keyterms_for, name_variants, wake_phrases_for


class WakePhrasesTest(unittest.TestCase):
    def test_compound_names_match_their_spoken_form(self):
        self.assertEqual(name_variants("RepMe"), ["repme", "rep me"])
        self.assertEqual(name_variants("Meeting Assistant"), ["meeting assistant", "meetingassistant"])
        self.assertEqual(name_variants("Bot2000"), ["bot2000", "bot 2000"])

    def test_aliases_are_added_once_and_cleaned(self):
        phrases = wake_phrases_for("Baas", ["Bass", "Bess", " bass ", "Ba'as!", ""])
        self.assertEqual(phrases, ["baas", "bass", "bess", "ba as"])

    def test_keyterms_keep_the_written_form(self):
        self.assertEqual(keyterms_for("RepMe", ["Rep Me", "repme"]), ["RepMe", "Rep Me"])
        self.assertEqual(keyterms_for("  "), [])


class WakePhraseRulesTest(unittest.TestCase):
    def test_unicode_letters_survive(self):
        self.assertEqual(name_variants("Émile Zoé"), ["émile zoé", "émilezoé"])

    def test_every_alias_is_kept_before_variants(self):
        aliases = [f"alias number {n}" for n in range(10)]
        phrases = wake_phrases_for("TheBot", aliases)
        for alias in aliases:
            self.assertIn(alias, phrases)
        self.assertEqual(phrases[0], "thebot")
        self.assertLessEqual(len(phrases), 12)

    def test_whole_word_match(self):
        from utils.wake_phrases import matches_wake_phrase

        self.assertTrue(matches_wake_phrase("Atlas Jones", ["atlas"]))
        self.assertTrue(matches_wake_phrase("hey, Rep Me!", ["rep me"]))
        self.assertFalse(matches_wake_phrase("Atlassian", ["atlas"]))
        self.assertFalse(matches_wake_phrase("", ["atlas"]))
