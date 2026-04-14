from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse

import httpx


CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
AUTH_URL = "https://app-api.pixiv.net/web/v1/login"
TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"


def create_oauth_session() -> tuple[str, str, str]:
    verifier = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(16)
    params = {
        "code_challenge": create_code_challenge(verifier),
        "code_challenge_method": "S256",
        "client": "pixiv-android",
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state,
    }
    return verifier, state, f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(*, code: str, code_verifier: str) -> dict:
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "include_policy": "true",
        "redirect_uri": REDIRECT_URI,
    }
    return post_token_request(data)


def refresh_access_token(refresh_token: str) -> dict:
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "include_policy": "true",
        "refresh_token": refresh_token,
    }
    return post_token_request(data)


def post_token_request(data: dict) -> dict:
    response = httpx.post(
        TOKEN_URL,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "App-OS": "android",
            "App-OS-Version": "11",
        },
        timeout=30.0,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("has_error") or payload.get("error"):
        raise RuntimeError(payload)
    return payload


def create_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def extract_code(value: str) -> str | None:
    if "accounts.pixiv.net/post-redirect" in value:
        parsed = urllib.parse.urlparse(value)
        query = urllib.parse.parse_qs(parsed.query)
        nested = query.get("return_to", [None])[0]
        if nested:
            current = nested
            seen: set[str] = set()
            while current and current not in seen:
                seen.add(current)
                if "code=" in current:
                    return extract_code(current)
                decoded = urllib.parse.unquote(current)
                if decoded == current:
                    break
                current = decoded
    if "code=" in value:
        parsed = urllib.parse.urlparse(value)
        query = urllib.parse.parse_qs(parsed.query)
        return query.get("code", [None])[0]
    if "://" in value or value.startswith("/"):
        return None
    return value or None


def looks_like_intermediate_redirect(value: str) -> bool:
    lowered = value.lower()
    return (
        "accounts.pixiv.net/post-redirect" in lowered or "/auth/pixiv/start" in lowered
    )
