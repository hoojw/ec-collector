"""Autofill-gated login recovery for the store browsers.

Several platforms land an authenticated profile on a login page before its
retained session is resumed.  Chrome usually holds a saved credential for that
exact page, so the page can complete itself once the submit control is pressed.

This module presses that control — and nothing else.  It never types, reads,
decrypts, or exports a credential, never handles a QR code, and never solves a
captcha.  The safety rule is explicit and enforced in one place:

    ``submit`` is only pressed when Chrome has already populated the password
    field itself (detected by *length only* — the value is never inspected).

If the field is still empty the module reports ``autofill_not_ready`` and stops.

Some pages then raise a step that only a human can clear — an SMS code box, or a
slider.  Those are *detected and reported*, never attempted: the store's
automated login stops, a warning goes to the log, and the fact is recorded under
``--state-dir`` so the next cycle does not press submit again.  Nothing is typed,
dragged, or scanned, and the record clears itself once the page leaves the login
landing.

Each platform is described by a :class:`PlatformProfile`.  Selectors were pinned
against the live login pages (2026-09-24); see ``deploy/LOGIN_RECOVERY.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
import websocket


@dataclass(frozen=True)
class HumanCheck:
    """A page state that only a human can clear (SMS code, QR scan, ...).

    Whether such a step appears is *conditional* — e.g. Meituan may ask for an
    SMS code after the slider, or may not.  So this is a probe, never a step in
    an assumed sequence.  When one is found the automated login for that store
    stops: pressing submit again would only deepen the risk controls.
    """

    kind: str
    # Structural signal first (stable ids survive CSS-module class churn).
    selectors: tuple[str, ...] = ()
    # Visible-text fallback, used only when the element is truly absent.
    markers: tuple[str, ...] = ()
    # Some flows leave the login origin entirely (Meituan's SMS step moves to
    # verify.meituan.com), so the live URL is matched as well.
    url_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformProfile:
    key: str
    label: str
    landing_host: str
    landing_path_prefixes: tuple[str, ...]
    # JD pins an approved post-login destination; the others accept any landing
    # on their known host+path.
    return_param: str
    approved_return_prefixes: tuple[str, ...]
    # Submit control: semantic CSS first (must match exactly one usable node),
    # exact visible text second.
    submit_selectors: tuple[str, ...]
    submit_texts: tuple[str, ...]
    # Used only to answer "did the browser fill the form?" via value length.
    password_selectors: tuple[str, ...]
    # Tmall's login form lives in a cross-origin iframe, so evaluation has to
    # target that frame's execution context.
    frame_origin_hint: str | None
    allow_submit: bool
    # Privacy-agreement checkbox. Empty means the platform has none, so the
    # agreement step is skipped entirely. The account owner opts in per platform:
    # ticking it accepts a legal agreement on their behalf.
    agreement_checkbox_selectors: tuple[str, ...] = ()
    accept_agreement: bool = False
    # Human-only page states; empty means none pinned for this platform yet.
    human_verification: tuple[HumanCheck, ...] = ()


JD = PlatformProfile(
    key="jd",
    label="京东",
    landing_host="passport.shop.jd.com",
    landing_path_prefixes=("/login/index.action/jdm",),
    return_param="ReturnUrl",
    approved_return_prefixes=(
        "https://rx.shop.jd.com/rx/rxInfo_list.action",
        "https://shop.jd.com/jdm/home",
    ),
    submit_selectors=(
        "button.password__submit",
        ".rcd-button--primary.password__submit",
        "form button.rcd-button--primary",
    ),
    submit_texts=("立即登录",),
    password_selectors=("input[type=password]",),
    frame_origin_hint=None,
    allow_submit=True,
)

MEITUAN = PlatformProfile(
    key="meituan",
    label="美团",
    landing_host="waimaie.meituan.com",
    landing_path_prefixes=("/new_fe/login_gw",),
    return_param="",
    approved_return_prefixes=(),
    submit_selectors=(
        "button.ep-login_btn",
        ".ep-login_btn",
        "button[type=submit]",
    ),
    submit_texts=("登录",),
    password_selectors=("input#password", "input[type=password]"),
    frame_origin_hint=None,
    allow_submit=True,
    # Verified 2026-09-24: the real checkbox is `input#checkbox` but it is 0x0;
    # the clickable element is the <label> inside `.ep-checkbox-container`.
    # Its class stays `selectChecked` while unchecked, so `.checked` is the only
    # trustworthy state signal.
    agreement_checkbox_selectors=("input#checkbox", "input[type=checkbox]"),
    accept_agreement=True,
    # Verified 2026-09-24 on a live occurrence: after the slider is solved the
    # page *may* raise an SMS step ("为了您的账户安全，请先验证手机") — it is
    # conditional and often absent. `#yodaVerification` / `#yodaTitle` are
    # stable ids; the `_sms__*` classes carry build hashes, so text is only the
    # fallback.
    # CAUTION: the login page itself has a 「验证码登录」 tab, so bare 「验证码」
    # must never be a marker — it would fire on every ordinary visit.
    human_verification=(
        HumanCheck(
            kind="sms",
            selectors=("input#yodaVerification", "div#yodaTitle"),
            markers=("请先验证手机", "获取验证码"),
            # Observed 2026-09-24: after the code is submitted the page leaves
            # the login origin and continues on verify.meituan.com.
            url_prefixes=("https://verify.meituan.com/",),
        ),
    ),
)

TMALL = PlatformProfile(
    key="tmall",
    label="天猫·生意参谋",
    # Verified 2026-09-24: sycm.taobao.com/custom/login.htm embeds
    # havanalogin.taobao.com/mini_login.htm, which holds the real form.
    landing_host="sycm.taobao.com",
    landing_path_prefixes=("/custom/login.htm",),
    return_param="_target",
    approved_return_prefixes=(),
    submit_selectors=(
        "button.fm-submit",
        ".fm-button.fm-submit",
        "button.password-login",
    ),
    submit_texts=("登录",),
    password_selectors=("input#fm-login-password", "input[type=password]"),
    frame_origin_hint="havanalogin.taobao.com",
    # Enabled 2026-09-24.  Submission stays gated by the autofill check: with no
    # saved credential Chrome fills nothing, so nothing is pressed and the run
    # reports autofill_not_ready.  Probed the live page: the real form sits in
    # the cross-origin iframe and carries no agreement checkbox.
    allow_submit=True,
    # Alibaba's nc (YunDun) slider.  It is already in the DOM but hidden until
    # the form is submitted, so only a *visible* one means it actually fired.
    human_verification=(
        HumanCheck(
            kind="captcha",
            selectors=("div#nc_1_wrapper", "span#nc_1_n1z"),
            markers=("请按住滑块", "拖动到最右"),
        ),
    ),
)

PLATFORMS: dict[str, PlatformProfile] = {p.key: p for p in (JD, MEITUAN, TMALL)}


def get_platform(key: str) -> PlatformProfile:
    try:
        return PLATFORMS[key]
    except KeyError:
        raise SystemExit(f"unknown platform: {key!r} (known: {', '.join(sorted(PLATFORMS))})")


# --------------------------------------------------------------------------
# Landing-page guards
# --------------------------------------------------------------------------

def is_login_landing(profile: PlatformProfile, url: str) -> bool:
    """True only for the exact login landing page this profile may act on."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != profile.landing_host:
        return False
    if profile.landing_path_prefixes and not any(
        parsed.path.startswith(prefix) for prefix in profile.landing_path_prefixes
    ):
        return False
    if profile.return_param and profile.approved_return_prefixes:
        return_value = (parse_qs(parsed.query).get(profile.return_param) or [""])[0]
        return any(return_value.startswith(p) for p in profile.approved_return_prefixes)
    return True


# --------------------------------------------------------------------------
# JavaScript
# --------------------------------------------------------------------------

AUTOFILL_JS = r"""
(() => {
  const SELECTORS = %(password_selectors)s;
  for (const sel of SELECTORS) {
    let nodes;
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    if (!nodes.length) continue;
    return {
      found: true,
      selector: sel,
      count: nodes.length,
      // Length only — the value is never read, returned, or logged.
      value_len: (nodes[0].value || '').length,
      visible: !!(nodes[0].getBoundingClientRect().width),
    };
  }
  return {found: false, value_len: 0};
})()
"""

# Resolves the submit control and (in click mode) presses it.  Kept as a single
# generated expression so the reported selector is exactly the one acted on.
_RESOLVER_JS = r"""
(() => {
  const SELECTORS = %(submit_selectors)s;
  const TEXTS = %(submit_texts)s;
  const usable = (n) => {
    if (n.disabled) return false;
    const r = n.getBoundingClientRect();
    return !!(r.width && r.height);
  };
  for (const sel of SELECTORS) {
    let nodes;
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    if (!nodes.length) continue;
    const ok = nodes.filter(usable);
    if (ok.length === 1) {
      return {found: true, how: 'css', selector: sel, tag: ok[0].tagName,
              text: (ok[0].innerText || '').trim().slice(0, 20), node: ok[0]};
    }
    if (ok.length > 1) {
      return {found: false, reason: 'ambiguous', how: 'css', selector: sel, count: ok.length};
    }
    return {found: false, reason: 'not_usable', how: 'css', selector: sel, count: nodes.length};
  }
  const all = Array.from(document.querySelectorAll('button'));
  for (const t of TEXTS) {
    const hit = all.filter(b => (b.innerText || '').trim() === t).filter(usable);
    if (hit.length === 1) {
      return {found: true, how: 'text', text: t, tag: hit[0].tagName, node: hit[0]};
    }
    if (hit.length > 1) {
      return {found: false, reason: 'ambiguous', how: 'text', text: t, count: hit.length};
    }
  }
  return {found: false, reason: 'not_found'};
})()
"""

_REPORT_ONLY_TAIL = r"""
  if (!pick.found) {
    return {found: false, reason: pick.reason, how: pick.how || null,
            selector: pick.selector || null, text: pick.text || null,
            count: pick.count || 0, clicked: false};
  }
  return {found: true, how: pick.how, selector: pick.selector || null,
          text: pick.text, tag: pick.tag, clicked: false};
})()"""

_CLICK_TAIL = r"""
  if (!pick.found) {
    return {found: false, reason: pick.reason, how: pick.how || null,
            selector: pick.selector || null, text: pick.text || null,
            count: pick.count || 0, clicked: false};
  }
  pick.node.click();
  return {found: true, how: pick.how, selector: pick.selector || null,
          text: pick.text, tag: pick.tag, clicked: true};
})()"""

# Centre of the account field, so a genuine input event can be dispatched.
FIELD_JS = r"""
(() => {
  const SELECTORS = %(password_selectors)s;
  const preferred = document.querySelector('input[type=text], input#login, input#loginname, input[type=email]');
  const target = preferred || (() => {
    for (const sel of SELECTORS) {
      const n = document.querySelector(sel);
      if (n) return n;
    }
    return null;
  })();
  if (!target) return {found: false};
  const r = target.getBoundingClientRect();
  return {found: true, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
          w: Math.round(r.width), h: Math.round(r.height)};
})()
"""

# Signals left by the page after a submit that did not navigate.
OUTCOME_JS = r"""
(() => {
  const grab = (sel) => Array.from(document.querySelectorAll(sel))
    .map(n => (n.innerText || '').trim()).filter(Boolean).slice(0, 6);
  const notices = [].concat(
    grab('[class*=toast]'), grab('[class*=Toast]'), grab('[class*=message]'),
    grab('[class*=Message]'), grab('[class*=tip]'), grab('[class*=Tip]'),
    grab('[class*=error]'), grab('[class*=Error]'), grab('[class*=alert]'));
  return {href: location.href, notices: notices.slice(0, 8)};
})()
"""


def _pick(resolver: str, tail: str) -> str:
    return "(() => {\n  const pick = " + resolver + ";\n" + tail


def resolve_expression(profile: PlatformProfile, mode: str) -> str:
    """Build the JS that resolves the submit control and optionally presses it.

    ``mode='probe'`` reports what would be pressed; ``mode='click'`` presses it.
    Both run the identical resolver, and the probe build contains no click
    statement at all, so "report only" cannot degrade into "act".
    """
    if mode not in {"probe", "click"}:
        raise ValueError(f"unknown mode: {mode!r}")
    resolver = _RESOLVER_JS % {
        "submit_selectors": json.dumps(list(profile.submit_selectors), ensure_ascii=False),
        "submit_texts": json.dumps(list(profile.submit_texts), ensure_ascii=False),
    }
    return _pick(resolver, _REPORT_ONLY_TAIL if mode == "probe" else _CLICK_TAIL)


def autofill_expression(profile: PlatformProfile) -> str:
    return AUTOFILL_JS % {
        "password_selectors": json.dumps(list(profile.password_selectors), ensure_ascii=False)
    }


def field_expression(profile: PlatformProfile) -> str:
    return FIELD_JS % {
        "password_selectors": json.dumps(list(profile.password_selectors), ensure_ascii=False)
    }


# The real checkbox is often a 0x0 input styled by a sibling <label>, so reading
# `.checked` is the only trustworthy state signal and the label is the click
# target.  Order: the associated label, an enclosing label, the input itself if
# it is actually rendered, otherwise its positioned parent.
AGREEMENT_JS = r"""
(() => {
  const SELECTORS = %(agreement_selectors)s;
  const vis = (n) => { const r = n.getBoundingClientRect(); return !!(r.width && r.height); };
  let box = null, used = null;
  for (const sel of SELECTORS) {
    let n = null;
    try { n = document.querySelector(sel); } catch (e) { continue; }
    if (n) { box = n; used = sel; break; }
  }
  if (!box) return {found: false};
  let label = box.id ? document.querySelector('label[for="' + box.id + '"]') : null;
  if (!label) label = box.closest('label');
  let target = null, how = null;
  if (label && vis(label)) { target = label; how = 'label'; }
  else if (vis(box)) { target = box; how = 'input'; }
  else if (box.parentElement && vis(box.parentElement)) { target = box.parentElement; how = 'parent'; }
  const r = target ? target.getBoundingClientRect() : null;
  return {
    found: true,
    selector: used,
    checked: !!box.checked,
    click_how: how,
    click_tag: target ? target.tagName : null,
    clickable: !!(r && r.width && r.height),
    x: r ? Math.round(r.x + r.width / 2) : null,
    y: r ? Math.round(r.y + r.height / 2) : null,
  };
})()
"""


def agreement_expression(profile: PlatformProfile) -> str:
    return AGREEMENT_JS % {
        "agreement_selectors": json.dumps(
            list(profile.agreement_checkbox_selectors), ensure_ascii=False
        )
    }


# A state the automation must not try to push through (SMS code, QR scan...).
# Detection only *reports*: it never types a code, drags a slider, or scans a
# QR image.  Structural selectors are tried first; visible text is the fallback
# for elements whose class names carry a build hash.
HUMAN_VERIFICATION_JS = r"""
(() => {
  const CHECKS = %(checks)s;
  const vis = (n) => {
    if (!n) return false;
    const r = n.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const s = getComputedStyle(n);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const bodyText = document.body ? document.body.innerText : '';
  for (const check of CHECKS) {
    for (const sel of check.selectors) {
      let n = null;
      try { n = document.querySelector(sel); } catch (e) { continue; }
      if (n && vis(n)) {
        return {required: true, kind: check.kind, how: 'selector', selector: sel,
                tag: n.tagName, text: (n.innerText || '').trim().slice(0, 60)};
      }
    }
    for (const marker of check.markers) {
      if (bodyText.includes(marker)) {
        return {required: true, kind: check.kind, how: 'marker', marker: marker};
      }
    }
  }
  return {required: false};
})()
"""


def human_verification_expression(profile: PlatformProfile) -> str:
    checks = [
        {"kind": c.kind, "selectors": list(c.selectors), "markers": list(c.markers)}
        for c in profile.human_verification
    ]
    return HUMAN_VERIFICATION_JS % {"checks": json.dumps(checks, ensure_ascii=False)}


# --------------------------------------------------------------------------
# CDP session
# --------------------------------------------------------------------------

class CdpSession:
    """Minimal CDP client that can evaluate inside a chosen frame context."""

    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=25, suppress_origin=True)
        self.message_id = 0
        self._events: list[dict] = []

    def send(self, method: str, params: dict | None = None) -> int:
        self.message_id += 1
        self.ws.send(json.dumps({"id": self.message_id, "method": method, "params": params or {}}))
        return self.message_id

    def drain(self, seconds: float = 2.5) -> None:
        deadline = time.monotonic() + seconds
        self.ws.settimeout(0.4)
        while time.monotonic() < deadline:
            try:
                message = json.loads(self.ws.recv())
            except Exception:
                continue
            if "id" not in message:
                self._events.append(message)

    def wait(self, request_id: int, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        self.ws.settimeout(1.0)
        while time.monotonic() < deadline:
            try:
                message = json.loads(self.ws.recv())
            except Exception:
                continue
            if message.get("id") == request_id:
                return message
            if "id" not in message:
                self._events.append(message)
        raise TimeoutError(f"no reply for CDP id={request_id}")

    def evaluate(self, expression: str, context_id: int | None = None) -> Any:
        params: dict[str, Any] = {"expression": expression, "returnByValue": True}
        if context_id is not None:
            params["contextId"] = context_id
        message = self.wait(self.send("Runtime.evaluate", params))
        result = message.get("result", {}).get("result", {})
        if "value" not in result:
            raise RuntimeError(result.get("description") or "page returned no value")
        return result["value"]

    def default_contexts(self) -> list[dict]:
        return [
            event["params"]["context"]
            for event in self._events
            if event.get("method") == "Runtime.executionContextCreated"
            and (event["params"]["context"].get("auxData") or {}).get("isDefault")
        ]

    def click_at(self, x: int, y: int) -> None:
        """Dispatch a genuine press/release pair, as a human click would."""
        for event_type in ("mousePressed", "mouseReleased"):
            self.wait(self.send("Input.dispatchMouseEvent", {
                "type": event_type, "x": x, "y": y,
                "button": "left", "clickCount": 1,
            }))

    def close(self) -> None:
        self.ws.close()


def find_login_target(profile: PlatformProfile, port: int) -> dict | None:
    targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=15).json()
    for target in targets:
        if target.get("type") == "page" and is_login_landing(profile, target.get("url", "")):
            return target
    return None


def _resolve_context(session: CdpSession, profile: PlatformProfile) -> int | None:
    """Default execution context to evaluate in (iframe profiles need the child)."""
    contexts = session.default_contexts()
    if profile.frame_origin_hint:
        for context in contexts:
            if profile.frame_origin_hint in (context.get("origin") or ""):
                return context["id"]
        return None
    for context in contexts:
        if not profile.frame_origin_hint:
            return context["id"]
    return None


# --------------------------------------------------------------------------
# Human-attention state
# --------------------------------------------------------------------------

# When a human-only state is hit, that store's automated login stops until the
# page leaves the login landing.  The record lives here so the next cycle can
# see it without repeating a submit that would only deepen the risk controls.
# The store cycles run as root, so a system path is writable.
DEFAULT_STATE_DIR = "/var/lib/subsidy-login"
ATTENTION_TTL_SECONDS = 6 * 3600


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))


def _attention_path(state_dir: str, profile: PlatformProfile, port: int) -> str:
    return os.path.join(state_dir, f"{profile.key}-{port}.json")


def _read_attention(state_dir: str, profile: PlatformProfile, port: int) -> dict | None:
    """Return the active attention record; an expired one is dropped."""
    try:
        with open(_attention_path(state_dir, profile, port), encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    age = time.time() - float(record.get("last_seen_epoch") or 0)
    if age > ATTENTION_TTL_SECONDS:
        _clear_attention(state_dir, profile, port)
        return None
    return record


def _write_attention(state_dir: str, profile: PlatformProfile, port: int,
                     kind: str, url: str, detail: dict) -> dict | None:
    """Remember that this store needs a human. Best-effort, never fatal."""
    path = _attention_path(state_dir, profile, port)
    try:
        with open(path, encoding="utf-8") as fh:
            previous = json.load(fh)
    except (OSError, ValueError):
        previous = {}
    now = time.time()
    record = {
        "platform": profile.key,
        "label": profile.label,
        "port": port,
        "kind": kind,
        "url": url,
        "reason": "login requires a human-only verification step",
        "first_seen": previous.get("first_seen") or _iso(now),
        "last_seen": _iso(now),
        "last_seen_epoch": now,
        "hits": int(previous.get("hits") or 0) + 1,
        "detail": detail,
    }
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        return None
    return record


def _clear_attention(state_dir: str, profile: PlatformProfile, port: int) -> bool:
    try:
        os.remove(_attention_path(state_dir, profile, port))
        return True
    except OSError:
        return False


def announce(profile: PlatformProfile, port: int, detail: dict) -> None:
    """Emit a warning line; journald keeps it at warning priority for triage."""
    print(
        f"LOGIN RECOVERY: {profile.label}({profile.key}) port {port} needs a human "
        f"[{detail.get('kind')}] at {detail.get('url')} "
        f"(detected by {detail.get('how')}: "
        f"{detail.get('selector') or detail.get('marker') or detail.get('url_prefix')}); "
        f"automated login for this store is stopped",
        file=sys.stderr,
        flush=True,
    )


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------

_RISK_WORDS = ("验证码", "滑块", "安全验证", "操作频繁", "异常访问", "访问受限", "请拖动")
_AGREEMENT_WORDS = ("协议", "同意", "勾选", "阅读并")

# How long to wait for the agreement box to report itself as ticked.
AGREEMENT_SETTLE_SECONDS = 3.0
AGREEMENT_POLL_SECONDS = 0.4


def _classify(notices: list[str]) -> tuple[str, list[str]]:
    joined = " ".join(notices)
    if any(word in joined for word in _RISK_WORDS):
        return "submitted_risk_control", notices
    if any(word in joined for word in _AGREEMENT_WORDS):
        return "submitted_agreement_required", notices
    return "submitted_needs_human_verification", notices


def inspect(profile: PlatformProfile, port: int, nudge: bool = True) -> dict:
    """Report the login-page state without pressing anything."""
    target = find_login_target(profile, port)
    if target is None:
        return {"state": "not_login_landing", "platform": profile.key}

    session = CdpSession(target["webSocketDebuggerUrl"])
    try:
        session.send("Runtime.enable")
        session.drain(2.0)
        context_id = _resolve_context(session, profile)
        if context_id is None:
            return {"state": "frame_not_found", "platform": profile.key,
                    "url": target.get("url"), "hint": profile.frame_origin_hint}

        autofill = session.evaluate(autofill_expression(profile), context_id)
        if nudge and not autofill.get("value_len"):
            autofill = _nudge_for_autofill(session, profile, context_id)

        submit = session.evaluate(resolve_expression(profile, "probe"), context_id)
        return {
            "state": "login_landing",
            "platform": profile.key,
            "url": target.get("url"),
            "allow_submit": profile.allow_submit,
            "autofill": autofill,
            "submit": {k: v for k, v in submit.items() if k != "node"},
        }
    finally:
        session.close()


def _nudge_for_autofill(session: CdpSession, profile: PlatformProfile, context_id: int) -> dict:
    """Give Chrome its cue to autofill: one genuine click on the account field.

    This is the same interaction a human performs.  No text is entered and no
    value is read back — only the resulting length is examined.
    """
    field = session.evaluate(field_expression(profile), context_id)
    if not field.get("found"):
        return session.evaluate(autofill_expression(profile), context_id)
    try:
        session.click_at(field["x"], field["y"])
    except Exception:
        return session.evaluate(autofill_expression(profile), context_id)
    deadline = time.monotonic() + 3.0
    probe = session.evaluate(autofill_expression(profile), context_id)
    while time.monotonic() < deadline and not probe.get("value_len"):
        time.sleep(0.5)
        probe = session.evaluate(autofill_expression(profile), context_id)
    return probe


def _ensure_agreement(session: CdpSession, profile: PlatformProfile, context_id: int,
                      state: dict | None = None) -> dict:
    """Tick the privacy-agreement box, but only if it is not already ticked.

    The account owner opted into this per platform: ticking the box accepts a
    legal agreement on their behalf.  The read happens first, so an already
    accepted page is left completely untouched.
    """
    current = state if state is not None else session.evaluate(
        agreement_expression(profile), context_id
    )
    if not current.get("found"):
        return {"state": "no_agreement_control"}
    if current.get("checked"):
        return {"state": "already_checked", "selector": current.get("selector"),
                "click_how": current.get("click_how")}
    if not current.get("clickable"):
        return {"state": "agreement_not_clickable", "selector": current.get("selector")}

    try:
        session.click_at(current["x"], current["y"])
    except Exception as exc:
        return {"state": "agreement_click_failed", "selector": current.get("selector"),
                "error": str(exc)[:120]}

    deadline = time.monotonic() + AGREEMENT_SETTLE_SECONDS
    while time.monotonic() < deadline:
        time.sleep(AGREEMENT_POLL_SECONDS)
        after = session.evaluate(agreement_expression(profile), context_id)
        if after.get("checked"):
            return {"state": "checked", "selector": after.get("selector"),
                    "click_how": after.get("click_how"), "click_tag": after.get("click_tag")}
    return {"state": "agreement_still_unchecked", "selector": current.get("selector")}


def _page_urls(port: int) -> list[str]:
    """Every page URL the debugger reports. Best-effort; [] when unreachable."""
    try:
        targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=15).json()
    except (requests.RequestException, ValueError):
        return []
    return [t.get("url") or "" for t in targets if t.get("type") == "page"]


def _url_human_verification(profile: PlatformProfile, urls: list[str]) -> dict:
    """Match live URLs against the declared human-only flows."""
    for check in profile.human_verification:
        for prefix in check.url_prefixes:
            for url in urls:
                if url and url.startswith(prefix):
                    return {"required": True, "kind": check.kind, "how": "url",
                            "url_prefix": prefix, "url": url}
    return {"required": False}


def _human_verification(session: CdpSession, profile: PlatformProfile,
                        context_id: int) -> dict:
    """Probe for a human-only step; a probe failure must never block recovery."""
    if not profile.human_verification:
        return {"required": False}
    try:
        return session.evaluate(human_verification_expression(profile), context_id)
    except Exception:
        return {"required": False}


def _attention_result(profile: PlatformProfile, port: int, human: dict, url: str,
                      state_dir: str | None, tracks: bool) -> dict:
    """Record the human step, log it, and report it as a stop condition."""
    detail = dict(human)
    detail["url"] = url
    record = None
    if tracks:
        record = _write_attention(state_dir, profile, port,
                                  human.get("kind") or "manual", url, human)
    announce(profile, port, detail)
    result = {
        "state": "human_verification_required",
        "platform": profile.key,
        "url": url,
        "kind": human.get("kind"),
        "how": human.get("how"),
        "selector": human.get("selector"),
        "marker": human.get("marker"),
        "url_prefix": human.get("url_prefix"),
        "observed": human.get("text"),
    }
    if record is not None:
        result["attention"] = {
            "path": _attention_path(state_dir, profile, port),
            "first_seen": record["first_seen"],
            "hits": record["hits"],
        }
    return result


def recover(
    profile: PlatformProfile,
    port: int,
    settle_seconds: int = 12,
    dry_run: bool = False,
    allow_submit: bool | None = None,
    nudge: bool = True,
    accept_agreement: bool | None = None,
    state_dir: str | None = DEFAULT_STATE_DIR,
) -> dict:
    """Detect a login landing page and, when safe, press its submit control."""
    submit_allowed = profile.allow_submit if allow_submit is None else allow_submit
    agreement_allowed = profile.accept_agreement if accept_agreement is None else accept_agreement
    has_agreement = bool(profile.agreement_checkbox_selectors)
    # Diagnostics never leave state behind.
    tracks = bool(state_dir) and bool(profile.human_verification) and not dry_run

    target = find_login_target(profile, port)
    if target is None:
        # No login landing is open — but the page may be sitting in a human-only
        # flow on another origin, which the target list still reports.
        human = _url_human_verification(profile, _page_urls(port))
        if human.get("required"):
            result = _attention_result(profile, port, human, human["url"],
                                       state_dir, tracks)
            result["agreement"] = None
            return result
        # Off the login landing, so any pending human step is resolved.
        if tracks:
            _clear_attention(state_dir, profile, port)
        return {"state": "not_login_landing", "platform": profile.key}

    session = CdpSession(target["webSocketDebuggerUrl"])
    try:
        session.send("Runtime.enable")
        session.drain(2.0)
        context_id = _resolve_context(session, profile)
        if context_id is None:
            return {"state": "frame_not_found", "platform": profile.key,
                    "url": target.get("url"), "hint": profile.frame_origin_hint}

        # The debugger's target URL lags behind client-side redirects, so the
        # live *top-level* href is what gets judged — and it is checked for a
        # human-only flow *before* the landing test, because that flow can move
        # to its own origin (Meituan continues on verify.meituan.com).
        # Tmall's form is cross-origin, so its frame href is checked as well.
        url = session.evaluate("location.href")
        probe_urls = [url]
        if profile.frame_origin_hint:
            probe_urls.append(session.evaluate("location.href", context_id))
        human = _url_human_verification(profile, probe_urls)
        if human.get("required"):
            result = _attention_result(profile, port, human,
                                       human.get("url") or url, state_dir, tracks)
            result["agreement"] = None
            return result

        if not is_login_landing(profile, url):
            if tracks:
                _clear_attention(state_dir, profile, port)
            return {"state": "not_login_landing", "platform": profile.key, "url": url}

        # A human-only step may also be embedded in the landing page itself —
        # e.g. the SMS box Meituan raises *after* the slider is solved.  It is
        # conditional, so this is a probe: not finding one means carry on.
        human = _human_verification(session, profile, context_id)
        if human.get("required"):
            result = _attention_result(profile, port, human, url, state_dir, tracks)
            result["agreement"] = None
            return result

        # A previous run already established that this store needs a human.
        # Nothing is pressed again until the page leaves the login landing.
        if tracks:
            pending = _read_attention(state_dir, profile, port)
            if pending:
                announce(profile, port, pending)
                return {"state": "awaiting_human", "platform": profile.key, "url": url,
                        "since": pending.get("first_seen"), "hits": pending.get("hits"),
                        "detail": pending.get("detail")}

        autofill = session.evaluate(autofill_expression(profile), context_id)
        if nudge and not autofill.get("value_len"):
            autofill = _nudge_for_autofill(session, profile, context_id)
        autofill_ready = bool(autofill.get("value_len"))

        # Read the agreement state whenever the platform has one.  Reading is
        # free of side effects; the box is only ever ticked on the submit path.
        agreement_state = session.evaluate(
            agreement_expression(profile), context_id
        ) if has_agreement else None

        # Resolve the submit control whenever the caller can see the result:
        # dry-run and detect-only diagnostics, and the autofill_not_ready alert
        # (so an operator can tell "selector expired" from "no credential").
        # Skipped only on the happy path, where it would be a wasted round-trip.
        want_submit_report = dry_run or not submit_allowed or not autofill_ready
        submit_report = None
        if want_submit_report:
            probe = session.evaluate(resolve_expression(profile, "probe"), context_id)
            submit_report = {k: v for k, v in probe.items() if k != "node"}

        def _agreement_payload(action: dict | None = None) -> dict | None:
            if not has_agreement:
                return None
            reported = dict(agreement_state or {})
            reported["accept_enabled"] = bool(agreement_allowed)
            if action is not None:
                reported["action"] = action["state"]
            return reported

        if not submit_allowed:
            return {"state": "detect_only", "platform": profile.key, "url": url,
                    "autofill_ready": autofill_ready, "submit": submit_report,
                    "agreement": _agreement_payload()}

        if not autofill_ready:
            # Safety gate: the browser has not filled the form, so there is
            # nothing legitimate to submit.  Never type credentials ourselves.
            return {"state": "autofill_not_ready", "platform": profile.key, "url": url,
                    "autofill": autofill, "submit": submit_report,
                    "agreement": _agreement_payload()}

        if dry_run:
            if not submit_report.get("found"):
                return {"state": "submit_not_found", "platform": profile.key, "url": url,
                        "reason": submit_report.get("reason"), "detail": submit_report,
                        "agreement": _agreement_payload()}
            would_check = bool(
                has_agreement and agreement_allowed and agreement_state
                and agreement_state.get("found") and not agreement_state.get("checked")
            )
            return {"state": "dry_run_would_submit", "platform": profile.key, "url": url,
                    "how": submit_report.get("how"), "selector": submit_report.get("selector"),
                    "text": submit_report.get("text"), "tag": submit_report.get("tag"),
                    "submit": submit_report, "would_check_agreement": would_check,
                    "agreement": _agreement_payload()}

        # Tick the agreement before submitting.  A box that is already ticked is
        # left alone, and a box that cannot be ticked is reported rather than
        # worked around.
        agreement_action = None
        if has_agreement and agreement_allowed:
            agreement_action = _ensure_agreement(session, profile, context_id, agreement_state)
            if agreement_action["state"] in {"agreement_not_clickable",
                                             "agreement_still_unchecked",
                                             "agreement_click_failed"}:
                return {"state": agreement_action["state"], "platform": profile.key,
                        "url": url, "agreement": _agreement_payload(agreement_action)}

        outcome = session.evaluate(resolve_expression(profile, "click"), context_id)
        if not outcome.get("found"):
            state = "submit_ambiguous" if outcome.get("reason") == "ambiguous" else "submit_not_found"
            return {"state": state, "platform": profile.key, "url": url,
                    "reason": outcome.get("reason"), "detail": outcome,
                    "agreement": _agreement_payload(agreement_action)}
        if not outcome.get("clicked"):
            return {"state": "submit_not_pressed", "platform": profile.key, "url": url,
                    "detail": outcome, "agreement": _agreement_payload(agreement_action)}

        deadline = time.monotonic() + settle_seconds
        while time.monotonic() < deadline:
            try:
                current = session.evaluate("location.href", context_id)
            except Exception:
                # Frame detached — the page navigated away, which is success.
                return {"state": "submitted_and_redirected", "platform": profile.key,
                        "url": "(frame navigated)", "selector": outcome.get("selector"),
                        "how": outcome.get("how"),
                        "agreement": _agreement_payload(agreement_action)}
            if not is_login_landing(profile, current):
                return {"state": "submitted_and_redirected", "platform": profile.key,
                        "url": current, "selector": outcome.get("selector"),
                        "how": outcome.get("how"),
                        "agreement": _agreement_payload(agreement_action)}
            # The step can appear right after the submit — e.g. behind a slider
            # the operator solves by hand — so keep watching through the settle
            # window instead of falling through to notice classification.
            human = _human_verification(session, profile, context_id)
            if human.get("required"):
                result = _attention_result(profile, port, human, current, state_dir, tracks)
                result["agreement"] = _agreement_payload(agreement_action)
                return result
            time.sleep(1)

        try:
            notices = session.evaluate(OUTCOME_JS, context_id).get("notices", [])
        except Exception:
            notices = []
        state, notices = _classify(notices)
        return {"state": state, "platform": profile.key, "url": url,
                "selector": outcome.get("selector"), "how": outcome.get("how"),
                "notices": notices, "agreement": _agreement_payload(agreement_action)}
    finally:
        session.close()


def capture(profile: PlatformProfile, port: int, path: str) -> dict:
    """Dump everything needed to pin selectors from a real occurrence."""
    target = find_login_target(profile, port)
    if target is None:
        return {"state": "not_login_landing", "platform": profile.key}

    session = CdpSession(target["webSocketDebuggerUrl"])
    try:
        session.send("Runtime.enable")
        session.send("Page.enable")
        session.drain(3.0)
        tree = session.wait(session.send("Page.getFrameTree"))
        contexts = session.default_contexts()
        dump: dict[str, Any] = {
            "platform": profile.key,
            "target_url": target.get("url"),
            "frames": [],
            "contexts": [],
            "probes": [],
        }

        def walk(node: dict) -> None:
            frame = node.get("frame", {})
            dump["frames"].append(frame.get("url"))
            for child in node.get("childFrames", []) or []:
                walk(child)

        walk(tree["result"]["frameTree"])
        for context in contexts:
            dump["contexts"].append({
                "id": context.get("id"), "origin": context.get("origin"),
                "name": context.get("name"),
            })
            try:
                dump["probes"].append(session.evaluate(OUTCOME_JS, context["id"]))
            except Exception as exc:
                dump["probes"].append({"error": str(exc)[:120]})
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(dump, handle, ensure_ascii=False, indent=2)
        return {"state": "captured", "platform": profile.key, "path": path,
                "contexts": len(contexts)}
    finally:
        session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect a platform login landing page and press its submit "
                    "control, but only when Chrome has already autofilled the form."
    )
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--settle-seconds", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be pressed, without pressing it")
    parser.add_argument("--detect-only", action="store_true",
                        help="report state only; overrides the platform default")
    parser.add_argument("--accept-agreement", dest="accept_agreement", action="store_true",
                        default=None,
                        help="tick the privacy-agreement box if it is not already ticked")
    parser.add_argument("--no-accept-agreement", dest="accept_agreement", action="store_false",
                        help="never touch the privacy-agreement box")
    parser.add_argument("--no-nudge", action="store_true",
                        help="do not click the account field to prompt autofill")
    parser.add_argument("--capture", metavar="PATH",
                        help="dump frames/contexts for offline selector pinning")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                        help="where the human-attention record is kept "
                             f"(default: {DEFAULT_STATE_DIR})")
    parser.add_argument("--no-state", action="store_true",
                        help="neither read nor write the human-attention record")
    args = parser.parse_args()
    if not 1 <= args.settle_seconds <= 60:
        parser.error("--settle-seconds must be between 1 and 60")
    return args


def main() -> None:
    args = parse_args()
    profile = get_platform(args.platform)
    if args.capture:
        result = capture(profile, args.port, args.capture)
    else:
        allow = False if args.detect_only else None
        result = recover(profile, args.port, args.settle_seconds,
                         dry_run=args.dry_run, allow_submit=allow,
                         nudge=not args.no_nudge,
                         accept_agreement=args.accept_agreement,
                         state_dir=None if args.no_state else args.state_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
