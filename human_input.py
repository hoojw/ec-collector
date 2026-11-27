#!/usr/bin/env python3
"""Human-like input primitives for CDP-driven collection.

Why this module exists
----------------------
The store collectors drive pages through ``Input.dispatchMouseEvent`` and
``Input.dispatchKeyEvent``.  Dispatched naively, those calls produce input that
no human hand can generate:

* a click arrives as ``mousePressed`` + ``mouseReleased`` in the same
  millisecond, with no ``mouseMoved`` before it -- a real cursor always travels
  to the target first, and the browser's own event pipeline emits at least one
  move on the way;
* keystrokes arrive back-to-back with zero inter-key delay, while a human types
  at roughly 80-350 ms per character with jitter;
* text appears via a single ``Input.insertText``, which fires no ``keydown``
  at all -- real typing produces a down/char/up triple per character.

Any risk engine that inspects the *shape* of the input event stream -- rather
than just its presence -- can separate dispatched input from typed input on
those three signals alone.  This module makes the stream look like a hand.

Design notes
------------
* All randomness comes from one ``random.Random`` instance so a caller can seed
  it for reproducible tests.
* Movement uses a quadratic Bezier arc with per-sample jitter, because a
  straight line of evenly spaced points is itself a tell.
* Timing follows a log-normal-ish distribution: human inter-key intervals are
  right-skewed (a long tail of hesitations), not uniform.
* The module is dependency-free and performs no I/O; it only speaks CDP.
"""

from __future__ import annotations

import math
import random
import time

# Printable ASCII that a keyboard can produce with a single key press.
_ASCII_KEYS = {chr(i): (chr(i), chr(i).lower(), chr(i).upper()) for i in range(32, 127)}

# Chinese IME / non-ASCII text cannot be typed key-by-key through
# dispatchKeyEvent without a real IME, so those runs fall back to insertText.

# Characters that need Shift on a US layout; Chrome derives modifiers itself
# from the text field, so we only need to name the physical key.
_NUMPAD_SAFE = set("abcdefghijklmnopqrstuvwxyz0123456789`-=[]\\;',./ ")


def _bezier(p0, p1, p2, t):
    """Quadratic Bezier point at parameter t."""
    x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t**2 * p2[0]
    y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t**2 * p2[1]
    return x, y


class HumanInput:
    """Dispatches hand-shaped mouse and keyboard input over a CDP page.

    ``page`` must expose a ``call(method, params)`` method -- the shape used by
    the existing store collectors.
    """

    def __init__(self, page, rng: random.Random | None = None, cursor=(0, 0)):
        self.page = page
        self.rng = rng or random.Random()
        self.x, self.y = cursor

    # -- internals ---------------------------------------------------------

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def _dispatch(self, event_type, x=None, y=None, **extra):
        params = {"type": event_type, "button": "left", "clickCount": 1}
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        params.update(extra)
        self.page.call("Input.dispatchMouseEvent", params)

    # -- mouse -------------------------------------------------------------

    def move_to(self, x: float, y: float, steps: int | None = None,
                overshoot: bool = True) -> None:
        """Travel the cursor to (x, y) along a curved, jittered path.

        ``overshoot`` adds a small overshoot-and-correct on the final approach,
        which is what ballistic human aiming actually looks like.
        """
        start = (self.x, self.y)
        target = (float(x), float(y))

        if overshoot and self.rng.random() < 0.6:
            # Aim a little past the target, then correct back onto it.
            dx, dy = target[0] - start[0], target[1] - start[1]
            length = math.hypot(dx, dy) or 1.0
            fudge = self.rng.uniform(8, 28)
            final = (target[0] + dx / length * fudge,
                     target[1] + dy / length * fudge)
        else:
            final = target

        if steps is None:
            distance = math.hypot(final[0] - start[0], final[1] - start[1])
            steps = max(6, min(40, int(distance / 22)))

        # Control point pulled off the straight line makes the arc non-obvious.
        mid = ((start[0] + final[0]) / 2, (start[1] + final[1]) / 2)
        bend = self.rng.uniform(-0.18, 0.18)
        control = (mid[0] + (final[1] - start[1]) * bend,
                   mid[1] - (final[0] - start[0]) * bend)

        for i in range(1, steps + 1):
            t = i / steps
            px, py = _bezier(start, control, final, t)
            # Per-sample tremor: a perfectly smooth curve is still synthetic.
            px += self.rng.uniform(-0.8, 0.8)
            py += self.rng.uniform(-0.8, 0.8)
            self._dispatch("mouseMoved", px, py)
            # Velocity profile: slower at the ends, faster in the middle.
            base = 0.012 if 0.15 < t < 0.85 else 0.026
            self._sleep(base * self.rng.uniform(0.6, 1.5))

        if overshoot and final != target:
            for i in range(1, 4):
                t = i / 3
                px = final[0] + (target[0] - final[0]) * t
                py = final[1] + (target[1] - final[1]) * t
                self._dispatch("mouseMoved", px, py)
                self._sleep(self.rng.uniform(0.01, 0.03))

        self.x, self.y = target

    def click(self, x: float = None, y: float = None, move: bool = True) -> None:
        """Click at (x, y), optionally walking the cursor there first."""
        if x is not None and y is not None:
            if move:
                self.move_to(x, y)
            else:
                self.x, self.y = float(x), float(y)
        # Settle before pressing, as a hand does.
        self._sleep(self.rng.uniform(0.05, 0.18))
        self._dispatch("mouseMoved", self.x, self.y)
        self._dispatch("mousePressed", self.x, self.y)
        # Real presses dwell; 30-110 ms is a normal tap.
        self._sleep(self.rng.uniform(0.03, 0.11))
        self._dispatch("mouseReleased", self.x, self.y)
        self._sleep(self.rng.uniform(0.08, 0.25))

    def hover(self, x: float, y: float) -> None:
        """Move onto a target and linger, without pressing."""
        self.move_to(x, y)
        self._sleep(self.rng.uniform(0.15, 0.6))

    def scroll(self, x: float, y: float, delta_y: float, ticks: int = 3) -> None:
        """Scroll in a few wheel ticks instead of one big jump."""
        self.move_to(x, y) if (x, y) != (self.x, self.y) else None
        remaining = delta_y
        for i in range(ticks):
            step = remaining / (ticks - i)
            remaining -= step
            self.page.call("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": self.x, "y": self.y,
                "deltaX": 0, "deltaY": step,
            })
            self._sleep(self.rng.uniform(0.04, 0.13))

    # -- keyboard ----------------------------------------------------------

    def _key_for(self, ch: str):
        """Map a character to (key, code, text) or None if it needs an IME."""
        if ch in _NUMPAD_SAFE:
            key = ch
            code = ("Key" + ch.upper()) if ch.isalpha() else {
                " ": "Space", "-": "Minus", "=": "Equal", "[": "BracketLeft",
                "]": "BracketRight", "\\": "Backslash", ";": "Semicolon",
                "'": "Quote", ",": "Comma", ".": "Period", "/": "Slash",
                "`": "Backquote",
            }.get(ch, "")
            if ch.isdigit():
                code = "Digit" + ch
            return key, code, ch
        if ch in _ASCII_KEYS:
            upper, lower, _ = _ASCII_KEYS[ch]
            key = ch
            code = ("Key" + ch.upper()) if ch.isalpha() else ""
            # Shifted punctuation: Chrome needs the unshifted key + Shift mod.
            shifted = "~!@#$%^&*()_+{}|:\"<>?"
            if ch in shifted:
                base = "`1234567890-=[]\\;',./"[shifted.index(ch)]
                return base, "", ch
            return key, code, ch
        return None

    def type_text(self, text: str, wpm: float | None = None) -> None:
        """Type ``text`` as a human would, one key at a time.

        Falls back to a single ``insertText`` only for runs that contain
        characters a physical keyboard cannot produce (Chinese, emoji, ...),
        because those genuinely require an IME and cannot be faked with
        key events.
        """
        if not text:
            return

        for ch in text:
            mapped = self._key_for(ch)
            if mapped is None:
                # Non-typable run: hand it to the IME-equivalent path.
                self.page.call("Input.insertText", {"text": ch})
                self._sleep(self.rng.uniform(0.09, 0.34))
                continue

            key, code, printable = mapped
            common = {"key": key, "code": code, "text": printable,
                      "unmodifiedText": printable,
                      "windowsVirtualKeyCode": ord(key.upper()) if key else 0,
                      "nativeVirtualKeyCode": ord(key.upper()) if key else 0}

            self.page.call("Input.dispatchKeyEvent",
                           {"type": "keyDown", **common})
            self.page.call("Input.dispatchKeyEvent",
                           {"type": "char", **common})
            self.page.call("Input.dispatchKeyEvent",
                           {"type": "keyUp", **common})

            # Inter-key interval: right-skewed around a comfortable pace.
            base = 60.0 / (wpm or self.rng.uniform(38, 62))  # seconds/char
            gap = base * self.rng.lognormvariate(0.0, 0.35)
            self._sleep(min(gap, 0.9))

            # Occasional thinking pause, as when re-reading a field.
            if self.rng.random() < 0.04:
                self._sleep(self.rng.uniform(0.35, 1.3))

    def press_key(self, key: str, code: str = "") -> None:
        """Press and release a named key (Enter, Tab, ArrowDown, ...)."""
        common = {"key": key, "code": code or key}
        self.page.call("Input.dispatchKeyEvent", {"type": "keyDown", **common})
        self._sleep(self.rng.uniform(0.03, 0.10))
        self.page.call("Input.dispatchKeyEvent", {"type": "keyUp", **common})
        self._sleep(self.rng.uniform(0.05, 0.16))

    # -- convenience -------------------------------------------------------

    def click_element(self, selector: str, timeout: float = 10.0) -> bool:
        """Move onto a DOM element's centre and click it like a hand would.

        Returns False if the element never materialised.  The bounding-box
        lookup keeps the caller from having to hardcode coordinates.
        """
        deadline = time.time() + timeout
        expr = (
            "(() => { const el = document.querySelector(%r);"
            " if (!el) return null;"
            " const r = el.getBoundingClientRect();"
            " if (r.width === 0 || r.height === 0) return null;"
            " return JSON.stringify({x: r.left + r.width / 2,"
            " y: r.top + r.height / 2}); })()" % selector
        )
        while time.time() < deadline:
            res = self.page.call("Runtime.evaluate", {
                "expression": expr, "returnByValue": True})
            value = (res.get("result", {}) or {}).get("value")
            if value:
                import json as _json
                box = _json.loads(value)
                # Aim at the centre but never at the exact mathematical centre.
                jx = box["x"] + self.rng.uniform(-0.22, 0.22) * 20
                jy = box["y"] + self.rng.uniform(-0.22, 0.22) * 8
                self.click(jx, jy)
                return True
            self._sleep(0.15)
        return False


def attach(page, seed: int | None = None) -> HumanInput:
    """Create a HumanInput bound to ``page``, optionally seeded for tests."""
    return HumanInput(page, rng=random.Random(seed))


if __name__ == "__main__":  # pragma: no cover - manual smoke helper
    import json
    import urllib.request

    class _Page:
        """Minimal CDP page used only for an interactive smoke check."""

        def __init__(self, port: int):
            target = json.load(urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}/json/new?about:blank",
                    method="PUT"), timeout=10))
            import websocket
            self.ws = websocket.create_connection(
                target["webSocketDebuggerUrl"], timeout=20)
            self._id = 0

        def call(self, method, params=None):
            self._id += 1
            self.ws.send(json.dumps({"id": self._id, "method": method,
                                     "params": params or {}}))
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("id") == self._id:
                    return msg.get("result", {})

    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
    human = attach(_Page(port), seed=7)
    human.move_to(400, 300)
    human.click(400, 300)
    print("smoke ok: cursor at", human.x, human.y)
