import unittest

from jd_login_page_recover import (
    LOGIN_BUTTON_SELECTORS,
    LOGIN_BUTTON_TEXTS,
    _resolve_expression,
    is_expected_passport_landing,
)


class PassportLandingGuardTest(unittest.TestCase):
    def test_accepts_only_the_expected_jd_passport_landing(self):
        self.assertTrue(is_expected_passport_landing(
            "https://passport.shop.jd.com/login/index.action/jdm?ReturnUrl="
            "https%3A%2F%2Frx.shop.jd.com%2Frx%2FrxInfo_list.action"
        ))
        self.assertTrue(is_expected_passport_landing(
            "https://passport.shop.jd.com/login/index.action/jdm?ReturnUrl="
            "https%3A%2F%2Fshop.jd.com%2Fjdm%2Fhome"
        ))


    def test_rejects_a_generic_jd_login_page_or_an_unrelated_return_url(self):
        self.assertFalse(is_expected_passport_landing("https://passport.jd.com/uc/login"))
        self.assertFalse(is_expected_passport_landing(
            "https://passport.shop.jd.com/login/index.action/jdm?ReturnUrl="
            "https%3A%2F%2Fjdsz.jd.com%2Fszweb%2Fview%2Fproduct%2Fproduct360.html"
        ))


class LoginButtonSelectorTest(unittest.TestCase):
    """The submit control must be located by semantic class, not layout position."""

    def test_primary_selector_is_the_verified_semantic_class(self):
        # Verified on the live passport page: this matches exactly one node.
        self.assertEqual(LOGIN_BUTTON_SELECTORS[0], "button.password__submit")

    def test_no_selector_relies_on_absolute_dom_position(self):
        # The previous eight-level XPath broke on any cosmetic reshuffle.
        for selector in LOGIN_BUTTON_SELECTORS:
            joined = "".join(selector.split())
            self.assertNotIn("div[", joined)
            self.assertNotIn("/html", joined)
            self.assertNotIn("nth-child", joined)

    def test_only_one_selector_can_match_a_bare_button(self):
        # A broad "form button.rcd-button--primary" style rule must stay last, and
        # nothing may degrade to a bare "button" (that also matches 无法登录).
        for selector in LOGIN_BUTTON_SELECTORS:
            self.assertNotEqual(selector.strip().lower(), "button")

    def test_text_fallback_excludes_the_neighbouring_retrieve_button(self):
        self.assertIn("立即登录", LOGIN_BUTTON_TEXTS)
        self.assertNotIn("无法登录", LOGIN_BUTTON_TEXTS)


class ResolveExpressionTest(unittest.TestCase):
    def test_rejects_an_unknown_mode(self):
        with self.assertRaises(ValueError):
            _resolve_expression("delete")

    def test_probe_and_click_share_one_resolver(self):
        probe = _resolve_expression("probe")
        click = _resolve_expression("click")
        # Same resolver body, only the post-resolution tail differs.
        self.assertIn("const pick =", probe)
        self.assertIn("const pick =", click)
        for selector in LOGIN_BUTTON_SELECTORS:
            self.assertIn(selector, probe)
            self.assertIn(selector, click)

    def test_probe_expression_contains_no_click_statement(self):
        # The report-only expression must not even contain a click statement, so
        # it cannot degrade into "act" if surrounding code is edited later.
        probe = _resolve_expression("probe")
        self.assertNotIn("pick.node.click()", probe)
        self.assertNotIn(".click(", probe)
        self.assertIn("pick.node.click()", _resolve_expression("click"))

    def test_embeds_text_fallback_readably(self):
        expr = _resolve_expression("probe")
        self.assertIn("立即登录", expr)
        self.assertNotIn("\\u7acb", expr)


if __name__ == "__main__":
    unittest.main()
