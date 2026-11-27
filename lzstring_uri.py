"""Minimal pure-Python LZ-string ``compressToEncodedURIComponent``.

JD's 商智 SPA keeps its page state in the query string as
``LZString.compressToEncodedURIComponent(JSON.stringify(state))`` — see the
product-360 bundle, which builds the product-detail drill-down URL that way.
Reproducing the encoder lets the collector mint a page URL for *any* date
window instead of depending on the hard-coded one the links happen to carry.

Ported from lz-string 1.4.4 by pieroxy (MIT licence).  Only the URI-safe
encoder is implemented; its alphabet is entirely URL-safe, so the result can be
placed in a query string without further escaping.  The decoder is deliberately
absent — nothing in this repository needs to read the parameter back.

The 172-character parameter shipped in the original product-detail link is used
as the regression vector in ``test_lzstring_uri.py``: compressing the decoded
JSON must reproduce it byte for byte.
"""

from __future__ import annotations

URI_SAFE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"
BITS_PER_CHAR = 6


class _BitWriter:
    """Emit ``count`` bits of ``value`` (least-significant first) into 6-bit chars."""

    def __init__(self) -> None:
        self.chars: list[str] = []
        self._value = 0
        self._position = 0

    def write(self, value: int, count: int) -> None:
        for _ in range(count):
            self._value = (self._value << 1) | (value & 1)
            if self._position == BITS_PER_CHAR - 1:
                self.chars.append(URI_SAFE_ALPHABET[self._value])
                self._value = 0
                self._position = 0
            else:
                self._position += 1
            value >>= 1

    def flush(self) -> None:
        """Pad the trailing partial character, which is how LZ-string ends a stream."""
        while True:
            self._value <<= 1
            if self._position == BITS_PER_CHAR - 1:
                self.chars.append(URI_SAFE_ALPHABET[self._value])
                return
            self._position += 1


def compress_to_encoded_uri_component(text: str) -> str:
    """Return the LZ-string URI-safe encoding of ``text``."""
    if text is None:
        return ""

    writer = _BitWriter()
    dictionary: dict[str, int] = {}
    pending: set[str] = set()
    dict_size = 3
    num_bits = 2
    enlarge_in = 2
    w = ""

    def emit_new_entry(entry: str) -> None:
        """Emit the literal for ``entry`` the first time it is seen."""
        code = ord(entry[0])
        if code < 256:
            writer.write(0, num_bits)
            writer.write(code, 8)
        else:
            writer.write(1, num_bits)
            writer.write(code, 16)

    def enlarge() -> None:
        nonlocal num_bits, enlarge_in
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

    for char in text:
        if char not in dictionary:
            dictionary[char] = dict_size
            dict_size += 1
            pending.add(char)
        wc = w + char
        if wc in dictionary:
            w = wc
            continue
        if w in pending:
            emit_new_entry(w)
            enlarge()
            pending.discard(w)
        else:
            writer.write(dictionary[w], num_bits)
        enlarge()
        dictionary[wc] = dict_size
        dict_size += 1
        w = char

    if w:
        if w in pending:
            emit_new_entry(w)
            enlarge()
            pending.discard(w)
        else:
            writer.write(dictionary[w], num_bits)
        enlarge()

    writer.write(2, num_bits)  # end-of-stream marker
    writer.flush()
    return "".join(writer.chars)


__all__ = ["URI_SAFE_ALPHABET", "compress_to_encoded_uri_component"]
