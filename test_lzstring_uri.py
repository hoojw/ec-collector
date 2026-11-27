import unittest

from lzstring_uri import URI_SAFE_ALPHABET, compress_to_encoded_uri_component

# The ``productDetailUrlParams`` JD's own 商品360 drill-down shipped, and the JSON
# it decodes to.  Compressing that JSON must reproduce the parameter exactly —
# this is what lets the collector mint a window for any date instead of inheriting
# whatever link an operator happened to copy.
SHIPPED_PARAM = (
    "N4IgDgTg9gKgnmApiAXCAzmAriANCAEwEMAXRMASwGMBrRCANSIBstkVQqsIJEA7EgGlEcVIQDseEFx78STVuwDaIAEwAGVQDYAtOoCcOgIwBmKRu17DR-SAC6"
    "+KlAC2YIr2Gi0ACwBGUp1d3RAU2VBULXQM9LXNNKOtVewBfZKA"
)
SHIPPED_STATE = (
    '{"proType":"spu","datepickerValue":{"currentKey":"d7",'
    '"currentValue":["2026-09-13","2026-09-19"],'
    '"compareKey":"hb","compareValue":["2026-09-06","2026-09-12"]}}'
)


class VectorTest(unittest.TestCase):
    def test_shipped_parameter_is_reproduced_byte_for_byte(self):
        self.assertEqual(compress_to_encoded_uri_component(SHIPPED_STATE), SHIPPED_PARAM)


class ShapeTest(unittest.TestCase):
    def test_output_uses_only_the_uri_safe_alphabet(self):
        encoded = compress_to_encoded_uri_component(SHIPPED_STATE)
        self.assertEqual(sorted(set(encoded) - set(URI_SAFE_ALPHABET)), [])

    def test_alphabet_has_no_characters_that_need_query_escaping(self):
        for char in "&=?#%/ ":
            self.assertNotIn(char, URI_SAFE_ALPHABET)

    def test_empty_input_still_returns_the_stream_marker(self):
        self.assertEqual(compress_to_encoded_uri_component(""), "Q")


if __name__ == "__main__":
    unittest.main()
