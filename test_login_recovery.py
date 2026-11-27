import os
import shutil
import tempfile
import unittest
from unittest import mock

import login_recovery
from login_recovery import (
    JD,
    MEITUAN,
    PLATFORMS,
    TMALL,
    _classify,
    autofill_expression,
    find_login_target,
    get_platform,
    is_login_landing,
    resolve_expression,
)


class LandingGuardTest(unittest.TestCase):
    def test_jd_accepts_only_approved_return_urls(self):
        self.assertTrue(is_login_landing(JD,
            "https://passport.shop.jd.com/login/index.action/jdm?ReturnUrl="
            "https%3A%2F%2Fshop.jd.com%2Fjdm%2Fhome"))
        self.assertTrue(is_login_landing(JD,
            "https://passport.shop.jd.com/login/index.action/jdm?ReturnUrl="
            "https%3A%2F%2Frx.shop.jd.com%2Frx%2FrxInfo_list.action"))
        # Unapproved post-login destination.
        self.assertFalse(is_login_landing(JD,
            "https://passport.shop.jd.com/login/index.action/jdm?ReturnUrl="
            "https%3A%2F%2Fjdsz.jd.com%2Fszweb%2Fview%2Fproduct%2Fproduct360.html"))
        # Different JD login host.
        self.assertFalse(is_login_landing(JD, "https://passport.jd.com/uc/login"))

    def test_meituan_landing(self):
        self.assertTrue(is_login_landing(
            MEITUAN, "https://waimaie.meituan.com/new_fe/login_gw#/login"))
        self.assertTrue(is_login_landing(
            MEITUAN, "https://waimaie.meituan.com/new_fe/login_gw"))
        self.assertFalse(is_login_landing(
            MEITUAN, "https://yiyao.meituan.com/main/frame#/page/home"))
        self.assertFalse(is_login_landing(
            MEITUAN, "https://waimaie.meituan.com/new_fe/other"))

    def test_tmall_landing(self):
        self.assertTrue(is_login_landing(TMALL,
            "https://sycm.taobao.com/custom/login.htm?_target="
            "http://sycm.taobao.com/portal/home.htm"))
        self.assertFalse(is_login_landing(
            TMALL, "https://sycm.taobao.com/portal/home.htm"))
        self.assertFalse(is_login_landing(
            TMALL, "https://havanalogin.taobao.com/mini_login.htm"))

    def test_requires_https(self):
        for profile, url in (
            (JD, "http://passport.shop.jd.com/login/index.action/jdm?ReturnUrl="
                 "https%3A%2F%2Fshop.jd.com%2Fjdm%2Fhome"),
            (MEITUAN, "http://waimaie.meituan.com/new_fe/login_gw"),
            (TMALL, "http://sycm.taobao.com/custom/login.htm"),
        ):
            self.assertFalse(is_login_landing(profile, url))


class ProfileShapeTest(unittest.TestCase):
    def test_tmall_submit_is_enabled_now_that_the_iframe_form_is_pinned(self):
        # The autofill gate still refuses to submit when Chrome has filled
        # nothing, so enabling this cannot push an uncredentialed profile.
        self.assertTrue(TMALL.allow_submit)
        self.assertTrue(JD.allow_submit)
        self.assertTrue(MEITUAN.allow_submit)

    def test_tmall_evaluates_inside_the_cross_origin_login_iframe(self):
        self.assertEqual(TMALL.frame_origin_hint, "havanalogin.taobao.com")
        self.assertIsNone(JD.frame_origin_hint)
        self.assertIsNone(MEITUAN.frame_origin_hint)

    def test_no_selector_relies_on_absolute_dom_position(self):
        for profile in PLATFORMS.values():
            for selector in profile.submit_selectors + profile.password_selectors:
                joined = "".join(selector.split())
                self.assertNotIn("div[", joined)
                self.assertNotIn("/html", joined)
                self.assertNotIn("nth-child", joined)

    def test_no_profile_degrades_to_a_bare_button_selector(self):
        for profile in PLATFORMS.values():
            for selector in profile.submit_selectors:
                self.assertNotEqual(selector.strip().lower(), "button")

    def test_jd_text_fallback_excludes_the_neighbouring_retrieve_button(self):
        self.assertIn("立即登录", JD.submit_texts)
        self.assertNotIn("无法登录", JD.submit_texts)

    def test_unknown_platform_is_rejected(self):
        with self.assertRaises(SystemExit):
            get_platform("taobao")


class ExpressionTest(unittest.TestCase):
    def test_rejects_an_unknown_mode(self):
        with self.assertRaises(ValueError):
            resolve_expression(JD, "delete")

    def test_probe_expression_contains_no_click_statement(self):
        for profile in PLATFORMS.values():
            probe = resolve_expression(profile, "probe")
            self.assertNotIn(".click(", probe)
            self.assertIn("pick.node.click()", resolve_expression(profile, "click"))

    def test_expressions_embed_the_platform_selectors(self):
        for profile in PLATFORMS.values():
            probe = resolve_expression(profile, "probe")
            for selector in profile.submit_selectors:
                self.assertIn(selector, probe)
            autofill = autofill_expression(profile)
            for selector in profile.password_selectors:
                self.assertIn(selector, autofill)

    def test_chinese_selectors_stay_readable(self):
        self.assertIn("立即登录", resolve_expression(JD, "probe"))
        self.assertNotIn("\\u7acb", resolve_expression(JD, "probe"))


class OutcomeClassificationTest(unittest.TestCase):
    def test_captcha_notice_maps_to_risk_control(self):
        state, notices = _classify(["请按住滑块，拖动到最右边"])
        self.assertEqual(state, "submitted_risk_control")
        self.assertEqual(notices, ["请按住滑块，拖动到最右边"])

    def test_agreement_notice_maps_to_agreement_required(self):
        self.assertEqual(_classify(["请先阅读并同意协议"])[0],
                         "submitted_agreement_required")
        self.assertEqual(_classify(["请勾选隐私协议"])[0],
                         "submitted_agreement_required")

    def test_unknown_notice_needs_a_human(self):
        self.assertEqual(_classify(["请输入账号"])[0],
                         "submitted_needs_human_verification")
        self.assertEqual(_classify([])[0], "submitted_needs_human_verification")

    def test_risk_wins_over_agreement_when_both_present(self):
        self.assertEqual(_classify(["请先同意协议", "请完成安全验证"])[0],
                         "submitted_risk_control")


class TargetLookupTest(unittest.TestCase):
    def _patch(self, targets):
        return mock.patch.object(login_recovery.requests, "get",
                                 return_value=mock.Mock(json=lambda: targets))

    def test_finds_only_the_matching_landing_page(self):
        with self._patch([
            {"type": "page", "url": "https://shop.jd.com/jdm/home"},
            {"type": "page", "url": "https://passport.shop.jd.com/login/index.action/jdm"
                                    "?ReturnUrl=https%3A%2F%2Fshop.jd.com%2Fjdm%2Fhome"},
        ]):
            target = find_login_target(JD, 9433)
        self.assertIsNotNone(target)
        self.assertIn("passport.shop.jd.com", target["url"])

    def test_returns_none_when_the_store_is_already_logged_in(self):
        with self._patch([{"type": "page", "url": "https://shop.jd.com/jdm/home"}]):
            self.assertIsNone(find_login_target(JD, 9433))

    def test_recover_reports_not_login_landing_without_touching_chrome(self):
        with self._patch([{"type": "page", "url": "https://shop.jd.com/jdm/home"}]):
            result = login_recovery.recover(JD, 9433)
        self.assertEqual(result["state"], "not_login_landing")
        self.assertEqual(result["platform"], "jd")

    def test_inspect_reports_not_login_landing_without_touching_chrome(self):
        with self._patch([{"type": "page", "url": "https://yiyao.meituan.com/"}]):
            self.assertEqual(login_recovery.inspect(MEITUAN, 9370)["state"],
                             "not_login_landing")


class AgreementControlDeclarationTest(unittest.TestCase):
    def test_only_meituan_declares_an_agreement_control(self):
        self.assertTrue(MEITUAN.agreement_checkbox_selectors)
        self.assertTrue(MEITUAN.accept_agreement)
        self.assertEqual(JD.agreement_checkbox_selectors, ())
        self.assertFalse(JD.accept_agreement)
        self.assertEqual(TMALL.agreement_checkbox_selectors, ())
        self.assertFalse(TMALL.accept_agreement)

    def test_agreement_expression_embeds_the_platform_selectors(self):
        expr = login_recovery.agreement_expression(MEITUAN)
        for selector in MEITUAN.agreement_checkbox_selectors:
            self.assertIn(selector, expr)
        # State must come from the checked property, not a styling class:
        # Meituan keeps class "selectChecked" even while unchecked.
        self.assertIn(".checked", expr)
        self.assertNotIn("className", expr)

    def test_agreement_expression_prefers_a_label_click_target(self):
        expr = login_recovery.agreement_expression(MEITUAN)
        self.assertIn("label[for=", expr)
        self.assertIn("closest('label')", expr)


class FakeSession:
    """Minimal stand-in for CdpSession, returning queued states in order."""

    def __init__(self, states):
        self.states = list(states)
        self.clicks = []

    def evaluate(self, expression, context_id=None):
        if not self.states:
            raise AssertionError("evaluate called more times than expected")
        return self.states.pop(0)

    def click_at(self, x, y):
        self.clicks.append((x, y))


class AgreementStepTest(unittest.TestCase):
    UNCHECKED = {"found": True, "selector": "input#checkbox", "checked": False,
                 "clickable": True, "click_how": "label", "click_tag": "LABEL",
                 "x": 629, "y": 454}
    CHECKED = {**UNCHECKED, "checked": True}

    def test_already_checked_is_left_completely_untouched(self):
        # The explicit requirement: read first, only tick when it is not ticked.
        session = FakeSession([])
        result = login_recovery._ensure_agreement(session, MEITUAN, 1, self.CHECKED)
        self.assertEqual(result["state"], "already_checked")
        self.assertEqual(session.clicks, [])

    def test_unchecked_is_ticked_and_verified(self):
        session = FakeSession([self.CHECKED])
        result = login_recovery._ensure_agreement(session, MEITUAN, 1, self.UNCHECKED)
        self.assertEqual(result["state"], "checked")
        self.assertEqual(session.clicks, [(629, 454)])
        self.assertEqual(result["click_how"], "label")

    def test_unchecked_is_read_when_no_state_was_supplied(self):
        session = FakeSession([self.UNCHECKED, self.CHECKED])
        result = login_recovery._ensure_agreement(session, MEITUAN, 1)
        self.assertEqual(result["state"], "checked")
        self.assertEqual(len(session.clicks), 1)

    def test_not_clickable_is_reported_not_worked_around(self):
        session = FakeSession([])
        result = login_recovery._ensure_agreement(
            session, MEITUAN, 1, {**self.UNCHECKED, "clickable": False})
        self.assertEqual(result["state"], "agreement_not_clickable")
        self.assertEqual(session.clicks, [])

    def test_missing_control_is_reported(self):
        session = FakeSession([])
        result = login_recovery._ensure_agreement(
            session, MEITUAN, 1, {"found": False})
        self.assertEqual(result["state"], "no_agreement_control")
        self.assertEqual(session.clicks, [])

    def test_click_failure_is_surfaced(self):
        class Boom(FakeSession):
            def click_at(self, x, y):
                raise RuntimeError("dispatch failed")

        result = login_recovery._ensure_agreement(Boom([]), MEITUAN, 1, self.UNCHECKED)
        self.assertEqual(result["state"], "agreement_click_failed")
        self.assertIn("dispatch failed", result["error"])

    def test_a_click_that_does_not_stick_is_reported(self):
        # Keep the retry window short so the suite stays fast.
        with mock.patch.object(login_recovery, "AGREEMENT_SETTLE_SECONDS", 0.2), \
             mock.patch.object(login_recovery, "AGREEMENT_POLL_SECONDS", 0.02):
            session = FakeSession([self.UNCHECKED] * 12)
            result = login_recovery._ensure_agreement(session, MEITUAN, 1, self.UNCHECKED)
        self.assertEqual(result["state"], "agreement_still_unchecked")
        self.assertEqual(len(session.clicks), 1)


class AgreementDryRunTest(unittest.TestCase):
    """dry-run must publish the agreement plan without touching the box."""

    def test_dry_run_reports_would_check_for_an_unchecked_box(self):
        target = {"type": "page",
                  "url": "https://waimaie.meituan.com/new_fe/login_gw#/login",
                  "webSocketDebuggerUrl": "ws://x"}
        unchecked = {"found": True, "selector": "input#checkbox", "checked": False,
                     "clickable": True, "click_how": "label", "x": 629, "y": 454,
                     "value_len": 0}
        filled = {"found": True, "selector": "input#password", "count": 1,
                  "value_len": 16, "visible": True}
        submit = {"found": True, "how": "css", "selector": "button.ep-login_btn",
                  "text": "登录", "tag": "BUTTON", "clicked": False}

        session = mock.MagicMock()
        session.default_contexts.return_value = [
            {"id": 7, "origin": "https://waimaie.meituan.com",
             "auxData": {"isDefault": True}}]
        session.evaluate.side_effect = [
            "https://waimaie.meituan.com/new_fe/login_gw#/login",  # location.href
            {"required": False},  # human-only step probe (absent)
            filled,            # autofill
            unchecked,         # agreement (read before nudging)
            submit,            # submit report
        ]
        session.click_at = mock.MagicMock()

        with mock.patch.object(login_recovery, "find_login_target", return_value=target), \
             mock.patch.object(login_recovery, "CdpSession", return_value=session):
            result = login_recovery.recover(MEITUAN, 9370, dry_run=True)

        self.assertEqual(result["state"], "dry_run_would_submit")
        self.assertTrue(result["would_check_agreement"])
        self.assertFalse(result["agreement"]["checked"])
        self.assertTrue(result["agreement"]["accept_enabled"])
        # A dry run must never actually tick the box (or click anything).
        session.click_at.assert_not_called()


class HumanVerificationDeclarationTest(unittest.TestCase):
    """The human-only stop is declared per platform and must not misfire."""

    def test_meituan_declares_the_sms_step_via_stable_ids(self):
        checks = MEITUAN.human_verification
        self.assertEqual([c.kind for c in checks], ["sms"])
        self.assertIn("input#yodaVerification", checks[0].selectors)

    def test_other_platforms_are_left_unset_until_pinned(self):
        # Only a platform whose human-only flow has actually been observed
        # declares one.
        self.assertEqual(JD.human_verification, ())

    def test_a_bare_captcha_word_is_never_a_marker(self):
        # The login page itself carries a 「验证码登录」 tab, so a bare 「验证码」
        # marker would fire on every ordinary visit.
        for check in MEITUAN.human_verification:
            self.assertNotIn("验证码", check.markers)

    def test_expression_embeds_the_declared_selectors_and_markers(self):
        expr = login_recovery.human_verification_expression(MEITUAN)
        self.assertIn("input#yodaVerification", expr)
        self.assertIn("请先验证手机", expr)

    def test_a_platform_without_a_step_produces_an_inert_probe(self):
        self.assertIn("const CHECKS = [];",
                      login_recovery.human_verification_expression(JD))


class AttentionRecordTest(unittest.TestCase):
    """The stop condition is remembered across runs and expires by itself."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_write_then_read_round_trip(self):
        record = login_recovery._write_attention(self.tmp, MEITUAN, 9333, "sms",
                                                 "https://x", {"kind": "sms"})
        self.assertIsNotNone(record)
        again = login_recovery._read_attention(self.tmp, MEITUAN, 9333)
        self.assertEqual(again["platform"], "meituan")
        self.assertEqual(again["kind"], "sms")
        self.assertEqual(again["hits"], 1)

    def test_repeated_hits_accumulate(self):
        for _ in range(3):
            login_recovery._write_attention(self.tmp, MEITUAN, 9333, "sms", "u", {})
        record = login_recovery._read_attention(self.tmp, MEITUAN, 9333)
        self.assertEqual(record["hits"], 3)

    def test_an_expired_record_is_dropped_and_removed(self):
        login_recovery._write_attention(self.tmp, MEITUAN, 9333, "sms", "u", {})
        with mock.patch.object(login_recovery, "ATTENTION_TTL_SECONDS", -1):
            self.assertIsNone(login_recovery._read_attention(self.tmp, MEITUAN, 9333))
        self.assertFalse(os.path.exists(
            login_recovery._attention_path(self.tmp, MEITUAN, 9333)))

    def test_clear_removes_the_record(self):
        login_recovery._write_attention(self.tmp, MEITUAN, 9333, "sms", "u", {})
        self.assertTrue(login_recovery._clear_attention(self.tmp, MEITUAN, 9333))
        self.assertIsNone(login_recovery._read_attention(self.tmp, MEITUAN, 9333))

    def test_records_are_isolated_by_port_and_platform(self):
        login_recovery._write_attention(self.tmp, MEITUAN, 9333, "sms", "u", {})
        self.assertIsNone(login_recovery._read_attention(self.tmp, MEITUAN, 9341))
        self.assertIsNone(login_recovery._read_attention(self.tmp, JD, 9333))

    def test_an_unwritable_state_dir_is_not_fatal(self):
        # A regular file where a directory is needed makes makedirs fail.
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        self.assertIsNone(login_recovery._write_attention(
            os.path.join(blocker, "nested"), MEITUAN, 9333, "sms", "u", {}))

    def test_a_missing_directory_reads_as_none(self):
        self.assertIsNone(login_recovery._read_attention(
            os.path.join(self.tmp, "nope"), MEITUAN, 9333))


class HumanStepRecoveryTest(unittest.TestCase):
    """Hitting the SMS step stops this store's automated login."""

    LANDING = "https://waimaie.meituan.com/new_fe/login_gw#/login"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _target(self):
        return {"type": "page", "url": self.LANDING, "webSocketDebuggerUrl": "ws://x"}

    def _session(self, *values):
        session = mock.MagicMock()
        session.default_contexts.return_value = [
            {"id": 7, "origin": "https://waimaie.meituan.com",
             "auxData": {"isDefault": True}}]
        session.evaluate.side_effect = list(values)
        session.click_at = mock.MagicMock()
        return session

    def _recover(self, session, **kwargs):
        with mock.patch.object(login_recovery, "find_login_target",
                               return_value=self._target()), \
             mock.patch.object(login_recovery, "CdpSession", return_value=session), \
             mock.patch.object(login_recovery, "announce") as announced:
            result = login_recovery.recover(MEITUAN, 9333, state_dir=self.tmp, **kwargs)
        return result, announced

    def test_a_present_step_is_reported_recorded_and_never_acted_on(self):
        sms = {"required": True, "kind": "sms", "how": "selector",
               "selector": "input#yodaVerification", "tag": "INPUT", "text": ""}
        session = self._session(self.LANDING, sms)
        result, announced = self._recover(session)

        self.assertEqual(result["state"], "human_verification_required")
        self.assertEqual(result["kind"], "sms")
        self.assertEqual(result["selector"], "input#yodaVerification")
        # Two evaluations only: the URL read and the probe. Nothing was pressed.
        self.assertEqual(len(session.evaluate.call_args_list), 2)
        session.click_at.assert_not_called()
        self.assertIsNotNone(login_recovery._read_attention(self.tmp, MEITUAN, 9333))
        announced.assert_called_once()

    def test_a_pending_record_blocks_submit_until_the_page_moves(self):
        login_recovery._write_attention(self.tmp, MEITUAN, 9333, "sms",
                                        self.LANDING, {"kind": "sms"})
        session = self._session(self.LANDING, {"required": False})
        result, announced = self._recover(session)

        self.assertEqual(result["state"], "awaiting_human")
        self.assertEqual(result["hits"], 1)
        self.assertEqual(len(session.evaluate.call_args_list), 2)
        session.click_at.assert_not_called()
        announced.assert_called_once()

    def test_leaving_the_landing_clears_the_record(self):
        login_recovery._write_attention(self.tmp, MEITUAN, 9333, "sms",
                                        self.LANDING, {"kind": "sms"})
        with mock.patch.object(login_recovery, "find_login_target", return_value=None), \
             mock.patch.object(login_recovery, "_page_urls", return_value=[]):
            result = login_recovery.recover(MEITUAN, 9333, state_dir=self.tmp)

        self.assertEqual(result["state"], "not_login_landing")
        self.assertIsNone(login_recovery._read_attention(self.tmp, MEITUAN, 9333))

    def test_the_step_appearing_after_submit_is_caught(self):
        filled = {"found": True, "value_len": 14}
        agreement = {"found": True, "selector": "input#checkbox", "checked": False,
                     "clickable": True, "click_how": "label", "x": 1, "y": 2}
        pressed = {"found": True, "clicked": True, "how": "css", "text": "登录",
                   "selector": "button.ep-login_btn", "tag": "BUTTON"}
        sms = {"required": True, "kind": "sms", "how": "marker",
               "marker": "请先验证手机"}
        session = self._session(self.LANDING, {"required": False}, filled, agreement,
                                pressed, self.LANDING, sms)
        result, _ = self._recover(session, settle_seconds=5, accept_agreement=False)

        self.assertEqual(result["state"], "human_verification_required")
        self.assertEqual(result["marker"], "请先验证手机")
        self.assertIsNotNone(login_recovery._read_attention(self.tmp, MEITUAN, 9333))

    def test_dry_run_reports_the_step_without_leaving_a_record(self):
        sms = {"required": True, "kind": "sms", "how": "marker",
               "marker": "请先验证手机"}
        session = self._session(self.LANDING, sms)
        result, _ = self._recover(session, dry_run=True)

        self.assertEqual(result["state"], "human_verification_required")
        self.assertIsNone(login_recovery._read_attention(self.tmp, MEITUAN, 9333))
        session.click_at.assert_not_called()

    def test_the_sms_flow_on_its_own_origin_stops_the_store(self):
        # The debugger target list still says "login page" while the live href
        # has already moved to the verify origin.
        session = self._session(
            "https://verify.meituan.com/sms/pcRegionalChoice.html?language=zh-CN")
        result, announced = self._recover(session)

        self.assertEqual(result["state"], "human_verification_required")
        self.assertEqual(result["how"], "url")
        self.assertEqual(result["url_prefix"], "https://verify.meituan.com/")
        self.assertEqual(len(session.evaluate.call_args_list), 1)
        session.click_at.assert_not_called()
        self.assertIsNotNone(login_recovery._read_attention(self.tmp, MEITUAN, 9333))
        announced.assert_called_once()


class HumanFlowUrlTest(unittest.TestCase):
    """Some human-only flows move to their own origin; the URL must catch them."""

    def test_meituan_declares_the_verify_origin(self):
        self.assertIn("https://verify.meituan.com/",
                      MEITUAN.human_verification[0].url_prefixes)

    def test_a_matching_url_is_reported(self):
        result = login_recovery._url_human_verification(
            MEITUAN,
            ["https://verify.meituan.com/sms/pcRegionalChoice.html?language=zh-CN"])
        self.assertTrue(result["required"])
        self.assertEqual(result["kind"], "sms")
        self.assertEqual(result["how"], "url")

    def test_the_login_landing_itself_is_not_matched(self):
        self.assertFalse(login_recovery._url_human_verification(
            MEITUAN, ["https://waimaie.meituan.com/new_fe/login_gw#/login"])["required"])

    def test_a_platform_without_url_rules_never_matches(self):
        self.assertFalse(login_recovery._url_human_verification(
            JD, ["https://verify.meituan.com/x"])["required"])

    def test_empty_urls_are_harmless(self):
        self.assertFalse(login_recovery._url_human_verification(MEITUAN, [""])["required"])


class TmallIframeTest(unittest.TestCase):
    """Tmall's form is cross-origin: the landing test must use the top URL."""

    TOP = ("https://sycm.taobao.com/custom/login.htm"
           "?_target=http://sycm.taobao.com/portal/home.htm")
    FRAME = ("https://havanalogin.taobao.com/mini_login.htm?lang=zh_cn&appName=taobao"
             "&notLoadSsoView=true&isMobile=false")

    def test_the_yundun_slider_is_declared(self):
        check = next((c for c in TMALL.human_verification if c.kind == "captcha"), None)
        self.assertIsNotNone(check)
        self.assertIn("div#nc_1_wrapper", check.selectors)

    def test_only_the_top_url_is_the_landing(self):
        # Reading location.href inside the iframe yields havanalogin.taobao.com,
        # which is not the landing host — so the top-level href must be used.
        self.assertTrue(is_login_landing(TMALL, self.TOP))
        self.assertFalse(is_login_landing(TMALL, self.FRAME))

    def test_recover_does_not_mistake_the_frame_for_a_foreign_page(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        target = {"type": "page", "url": self.TOP, "webSocketDebuggerUrl": "ws://x"}
        autofill = {"found": True, "selector": "input#fm-login-password",
                    "count": 1, "value_len": 0, "visible": True}
        report = {"found": False, "reason": "not_found", "how": None, "selector": None,
                  "text": None, "count": 0, "clicked": False}
        session = mock.MagicMock()
        session.default_contexts.return_value = [
            {"id": 1, "origin": "https://sycm.taobao.com", "auxData": {"isDefault": True}},
            {"id": 2, "origin": "https://havanalogin.taobao.com",
             "auxData": {"isDefault": False}},
        ]
        session.evaluate.side_effect = [
            self.TOP,             # top-level location.href
            self.FRAME,           # iframe location.href
            {"required": False},  # element/text probe
            autofill,             # no saved credential in this profile
            report,               # submit report
        ]
        session.click_at = mock.MagicMock()

        with mock.patch.object(login_recovery, "find_login_target", return_value=target), \
             mock.patch.object(login_recovery, "CdpSession", return_value=session):
            result = login_recovery.recover(TMALL, 9634, state_dir=tmp, nudge=False)

        # The gate reports the missing credential — it must not be mistaken for
        # "this page is not a login page", which the frame href would suggest.
        self.assertEqual(result["state"], "autofill_not_ready")
        self.assertEqual(result["url"], self.TOP)
        session.click_at.assert_not_called()


if __name__ == "__main__":
    unittest.main()
