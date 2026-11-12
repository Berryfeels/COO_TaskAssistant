"""Direct unit tests for the near-duplicate guard's primitives.

test_duplicates.py covers the guard through the pipeline; these tests pin
down normalize/similarity/is_similar themselves (thresholds, punctuation,
accents, empty input).
"""
import unittest

from app.similarity import is_similar, normalize, similarity


class TestNormalize(unittest.TestCase):
    def test_case_and_punctuation_folded(self):
        self.assertEqual(normalize("Call the SUPPLIER!!!"), "call the supplier")

    def test_whitespace_collapsed(self):
        self.assertEqual(normalize("  call   the \tsupplier "), "call the supplier")

    def test_accents_kept(self):
        self.assertEqual(normalize("per Marco è urgente"), "per marco è urgente")

    def test_numbers_kept(self):
        self.assertEqual(normalize("Q2 report 2026"), "q2 report 2026")

    def test_only_punctuation_gives_empty(self):
        self.assertEqual(normalize("!!! ... ???"), "")


class TestSimilarity(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertEqual(similarity("Call supplier", "call supplier"), 1.0)

    def test_empty_is_zero(self):
        self.assertEqual(similarity("", "anything"), 0.0)
        self.assertEqual(similarity("!!!", "anything"), 0.0)  # normalizes empty

    def test_reworded_repeat_still_similar(self):
        self.assertGreaterEqual(
            similarity("Call the supplier", "call the supplier!!"), 0.75)

    def test_unrelated_titles_below_threshold(self):
        self.assertLess(
            similarity("Call the supplier", "Send the invoice"), 0.75)

    def test_word_reorder_drops_below_threshold(self):
        # difflib is order-sensitive: a reordering is not flagged
        self.assertLess(similarity("invoice send the", "send the invoice"), 0.75)


class TestIsSimilar(unittest.TestCase):
    def test_flags_at_threshold(self):
        self.assertTrue(is_similar("call supplier", "call supplier"))

    def test_rejects_below_threshold(self):
        self.assertFalse(is_similar("call the supplier", "x y z w"))

    def test_custom_threshold(self):
        pair = ("call the supplier", "call the customer")
        self.assertFalse(is_similar(*pair, threshold=0.8))
        self.assertTrue(is_similar(*pair, threshold=0.5))
