#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import webbrowser

from fanfictl.pixiv_oauth import (
    create_oauth_session,
    exchange_code_for_token,
    extract_code,
    refresh_access_token,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Get a Pixiv refresh token for Fableport"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="Start Pixiv OAuth login flow")
    login_parser.add_argument(
        "--no-browser", action="store_true", help="Do not auto-open the browser"
    )
    login_parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="Print live tokens in full (unsafe; avoid in shared terminals/logs)",
    )

    refresh_parser = subparsers.add_parser(
        "refresh", help="Exchange an existing refresh token"
    )
    refresh_parser.add_argument("refresh_token", help="Existing Pixiv refresh token")
    refresh_parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="Print live tokens in full (unsafe; avoid in shared terminals/logs)",
    )

    args = parser.parse_args()
    if args.command == "login":
        return run_login(no_browser=args.no_browser, show_secrets=args.show_secrets)
    if args.command == "refresh":
        return run_refresh(args.refresh_token, show_secrets=args.show_secrets)
    parser.print_help()
    return 1


def run_login(*, no_browser: bool, show_secrets: bool) -> int:
    verifier, _state, url = create_oauth_session()

    print("Open this URL and sign into Pixiv:")
    print(url)
    print()
    print("After login, Pixiv redirects to a callback URL containing ?code=...")
    print("Paste either the full callback URL or just the code below.")
    print("The code expires quickly, so do it immediately.")
    print()

    if not no_browser:
        webbrowser.open(url)

    pasted = input("Callback URL or code: ").strip()
    code = extract_code(pasted)
    if not code:
        print("Could not extract a Pixiv OAuth code from your input.", file=sys.stderr)
        return 2

    payload = exchange_code_for_token(code=code, code_verifier=verifier)
    print_token_result(payload, show_secrets=show_secrets)
    return 0


def run_refresh(refresh_token: str, show_secrets: bool) -> int:
    payload = refresh_access_token(refresh_token)
    print_token_result(payload, show_secrets=show_secrets)
    return 0


def print_token_result(payload: dict, *, show_secrets: bool) -> None:
    print()
    print("Pixiv OAuth succeeded.")
    print()
    print("refresh_token:")
    refresh_token = payload.get("refresh_token", "")
    print(refresh_token if show_secrets else mask_secret(refresh_token))
    print()
    print(
        "Use the refresh_token in Fableport Settings or in .env as PIXIV_REFRESH_TOKEN."
    )
    if show_secrets:
        print()
        print("access_token:")
        print(payload.get("access_token", ""))
    else:
        print("Use --show-secrets if you really need the raw tokens printed.")


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:8]}…{value[-4:]}"


if __name__ == "__main__":
    raise SystemExit(main())
