#!/usr/bin/env python3
"""Tests for the human-like input primitives."""

import random
import unittest

from human_input import HumanInput


class FakePage:
    """Records CDP calls so tests can assert on the event stream shape."""

    def __init__(self):
        self.calls = []
        self.evaluations = []
        self.next_value = None

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "Runtime.evaluate":
            self.evaluations.append(params)
            if self.next_value is not None:
                return {"result": {"value": self.next_value}}
            return {"result": {"value": None}}
        return {}

    def events(self, name):
        return [p for m, p in self.calls if m == name]


class MoveTest(unittest.TestCase):
    def test_move_emits_only_mouse_moved(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(1))
        human.move_to(300, 200)
        moves = page.events("Input.dispatchMouseEvent")
        self.assertTrue(moves)
        self.assertTrue(all(m["type"] == "mouseMoved" for m in moves))

    def test_move_produces_many_intermediate_points(self):
        """A real cursor travels; a synthetic one jumps. Assert the path."""
        page = FakePage()
        human = HumanInput(page, rng=random.Random(2))
        human.move_to(800, 600)
        moves = page.events("Input.dispatchMouseEvent")
        # A straight jump would be 1 event; a hand produces a path.
        self.assertGreaterEqual(len(moves), 6)

    def test_move_ends_at_target(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(3))
        human.move_to(123, 456)
        last = page.events("Input.dispatchMouseEvent")[-1]
        self.assertAlmostEqual(last["x"], 123, delta=0.001)
        self.assertAlmostEqual(last["y"], 456, delta=0.001)

    def test_move_path_is_not_a_straight_line(self):
        """Jitter/curvature must make the path measurably non-linear."""
        page = FakePage()
        human = HumanInput(page, rng=random.Random(4))
        human.move_to(500, 0) if False else None
        human = HumanInput(page, rng=random.Random(4))
        human.move_to(400, 0)
        pts = [(p["x"], p["y"]) for p in page.events("Input.dispatchMouseEvent")]
        # Off-axis deviation from the y=0 line proves curvature.
        max_dev = max(abs(y) for _, y in pts)
        self.assertGreater(max_dev, 0.1)


class ClickTest(unittest.TestCase):
    def test_click_is_press_then_release_with_move_before(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(5))
        human.click(250, 150)
        events = page.events("Input.dispatchMouseEvent")
        types = [e["type"] for e in events]
        self.assertIn("mouseMoved", types)
        pressed = types.index("mousePressed")
        released = types.index("mouseReleased")
        self.assertLess(pressed, released)
        # At least one move must precede the press.
        self.assertTrue(any(t == "mouseMoved" for t in types[:pressed]))

    def test_click_without_move_skips_travel(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(6))
        human.click(10, 10, move=False)
        types = [e["type"] for e in page.events("Input.dispatchMouseEvent")]
        self.assertIn("mousePressed", types)
        # No long travel path, but a pre-press positioning move is still fine.
        self.assertLess(len([t for t in types if t == "mouseMoved"]), 4)

    def test_click_uses_left_button_with_count_one(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(7))
        human.click(50, 50)
        for e in page.events("Input.dispatchMouseEvent"):
            self.assertEqual(e["button"], "left")
            self.assertEqual(e["clickCount"], 1)


class TypeTest(unittest.TestCase):
    def test_typing_emits_down_char_up_triples(self):
        """Real typing fires keydown+char+keyup; insertText does not."""
        page = FakePage()
        human = HumanInput(page, rng=random.Random(8))
        human.type_text("abc")
        types = [e["type"] for e in page.events("Input.dispatchKeyEvent")]
        self.assertEqual(types.count("keyDown"), 3)
        self.assertEqual(types.count("char"), 3)
        self.assertEqual(types.count("keyUp"), 3)
        # And no whole-string insert.
        self.assertEqual(page.events("Input.insertText"), [])

    def test_typing_order_per_character_is_down_char_up(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(9))
        human.type_text("ab")
        types = [e["type"] for e in page.events("Input.dispatchKeyEvent")]
        self.assertEqual(types, ["keyDown", "char", "keyUp"] * 2)

    def test_typing_carries_key_and_code(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(10))
        human.type_text("k")
        down = page.events("Input.dispatchKeyEvent")[0]
        self.assertEqual(down["key"], "k")
        self.assertEqual(down["code"], "KeyK")
        self.assertEqual(down["text"], "k")

    def test_digits_and_space_get_correct_codes(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(11))
        human.type_text("4 ")
        downs = [e for e in page.events("Input.dispatchKeyEvent")
                 if e["type"] == "keyDown"]
        self.assertEqual(downs[0]["code"], "Digit4")
        self.assertEqual(downs[1]["code"], "Space")

    def test_non_ascii_falls_back_to_insert_text(self):
        """Chinese cannot be produced by a US keyboard, so IME path is used."""
        page = FakePage()
        human = HumanInput(page, rng=random.Random(12))
        human.type_text("药品")
        self.assertEqual(len(page.events("Input.insertText")), 2)
        self.assertEqual(page.events("Input.insertText")[0]["text"], "药")

    def test_empty_string_is_a_noop(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(13))
        human.type_text("")
        self.assertEqual(page.calls, [])

    def test_seeded_runs_are_reproducible(self):
        """Same seed must yield the same key sequencing decisions."""
        def run(seed):
            p = FakePage()
            HumanInput(p, rng=random.Random(seed)).type_text("abc123")
            return [e["type"] for e in p.events("Input.dispatchKeyEvent")]
        self.assertEqual(run(42), run(42))


class PressKeyTest(unittest.TestCase):
    def test_press_key_is_down_then_up(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(14))
        human.press_key("Enter", "Enter")
        types = [e["type"] for e in page.events("Input.dispatchKeyEvent")]
        self.assertEqual(types, ["keyDown", "keyUp"])


class ElementTest(unittest.TestCase):
    def test_click_element_uses_bounding_box(self):
        page = FakePage()
        page.next_value = '{"x": 100, "y": 200}'
        human = HumanInput(page, rng=random.Random(15))
        self.assertTrue(human.click_element("#submit", timeout=1))
        self.assertTrue(page.evaluations)

    def test_click_element_returns_false_when_missing(self):
        page = FakePage()
        page.next_value = None
        human = HumanInput(page, rng=random.Random(16))
        self.assertFalse(human.click_element("#nope", timeout=0.4))


class ScrollTest(unittest.TestCase):
    def test_scroll_uses_multiple_ticks(self):
        page = FakePage()
        human = HumanInput(page, rng=random.Random(17))
        human.scroll(100, 100, 600, ticks=4)
        wheels = [e for e in page.events("Input.dispatchMouseEvent")
                  if e["type"] == "mouseWheel"]
        self.assertEqual(len(wheels), 4)
        self.assertAlmostEqual(sum(w["deltaY"] for w in wheels), 600, delta=0.001)


if __name__ == "__main__":
    unittest.main()
