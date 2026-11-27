"""JD entry point for login recovery.  Thin wrapper over :mod:`login_recovery`.

Kept as a separate module because ``run-jd-store-cycle.sh`` calls it by name and
the JD landing-page guard has its own contract.  All behaviour now lives in
``login_recovery`` so every platform shares one engine, one safety gate, and one
set of states.
"""

from __future__ import annotations

import argparse
import json

from login_recovery import (
    JD,
    PLATFORMS,
    autofill_expression,
    capture,
    get_platform,
    inspect,
    is_login_landing,
    recover as _recover,
    resolve_expression,
)

# Backwards-compatible names: the JD submit control and its exact-text fallback.
LOGIN_BUTTON_SELECTORS = JD.submit_selectors
LOGIN_BUTTON_TEXTS = JD.submit_texts

__all__ = [
    "JD",
    "LOGIN_BUTTON_SELECTORS",
    "LOGIN_BUTTON_TEXTS",
    "autofill_expression",
    "capture",
    "inspect",
    "is_expected_passport_landing",
    "recover",
    "resolve_expression",
    "_resolve_expression",
]


def is_expected_passport_landing(url: str) -> bool:
    """True only for the JD passport landing page this helper may act on."""
    return is_login_landing(JD, url)


def _resolve_expression(mode: str) -> str:
    """JD-specific generated JS; see :func:`login_recovery.resolve_expression`."""
    return resolve_expression(JD, mode)


def recover(port: int, settle_seconds: int, dry_run: bool = False, **kwargs) -> dict:
    """Recover a JD store home from the passport landing page.

    ``submit`` is pressed only after Chrome itself has populated the password
    field; see :func:`login_recovery.recover`.
    """
    return _recover(JD, port, settle_seconds=settle_seconds, dry_run=dry_run, **kwargs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Click only the JD passport landing-page login control"
    )
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--settle-seconds", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true",
                        help="report which control would be pressed, without pressing it")
    parser.add_argument("--capture", metavar="PATH",
                        help="dump frames/contexts for offline selector pinning")
    args = parser.parse_args()
    if not 1 <= args.settle_seconds <= 60:
        parser.error("--settle-seconds must be between 1 and 60")
    return args


def main() -> None:
    args = parse_args()
    if args.capture:
        result = capture(JD, args.port, args.capture)
    else:
        result = recover(args.port, args.settle_seconds, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
