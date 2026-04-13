from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from fanfictl.auth import UserRecord, UserStore
from fanfictl.config import Settings


def pixiv_token_id_for(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def mask_pixiv_token(value: str) -> str:
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:8]}…{value[-4:]}"


@dataclass
class RuntimePixivToken:
    id: str
    refresh_token: str
    source: str
    is_default: bool
    owner_user_id: int | None = None
    owner_username: str | None = None


@dataclass
class PixivTokenSummary:
    id: str
    source: str
    masked: str
    is_default: bool
    owner_user_id: int | None
    owner_username: str | None
    created_at: str | None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class PixivTokenStore:
    def __init__(self, settings: Settings, user_store: UserStore | None = None) -> None:
        self.settings = settings
        self.user_store = user_store or UserStore(settings)

    def runtime_tokens_for_user(
        self, user: UserRecord | None
    ) -> list[RuntimePixivToken]:
        tokens: list[RuntimePixivToken] = []
        seen: set[str] = set()

        if user:
            for row in self._fetch_rows(owner_user_id=user.id, scope="user"):
                token_id = pixiv_token_id_for(row["refresh_token"])
                if token_id in seen:
                    continue
                tokens.append(
                    RuntimePixivToken(
                        id=token_id,
                        refresh_token=row["refresh_token"],
                        source="personal",
                        is_default=False,
                        owner_user_id=user.id,
                        owner_username=user.username,
                    )
                )
                seen.add(token_id)

        for row in self._fetch_rows(owner_user_id=None, scope="global"):
            token_id = pixiv_token_id_for(row["refresh_token"])
            if token_id in seen:
                continue
            tokens.append(
                RuntimePixivToken(
                    id=token_id,
                    refresh_token=row["refresh_token"],
                    source="global",
                    is_default=False,
                )
            )
            seen.add(token_id)

        if self.settings.pixiv_refresh_token:
            token_id = pixiv_token_id_for(self.settings.pixiv_refresh_token)
            if token_id not in seen:
                tokens.append(
                    RuntimePixivToken(
                        id=token_id,
                        refresh_token=self.settings.pixiv_refresh_token,
                        source="system",
                        is_default=True,
                    )
                )
        return tokens

    def list_personal_tokens(self, user: UserRecord) -> list[PixivTokenSummary]:
        return [
            PixivTokenSummary(
                id=pixiv_token_id_for(row["refresh_token"]),
                source="personal",
                masked=mask_pixiv_token(row["refresh_token"]),
                is_default=False,
                owner_user_id=user.id,
                owner_username=user.username,
                created_at=row["created_at"],
            )
            for row in self._fetch_rows(owner_user_id=user.id, scope="user")
        ]

    def list_global_tokens(self) -> list[PixivTokenSummary]:
        summaries = [
            PixivTokenSummary(
                id=pixiv_token_id_for(row["refresh_token"]),
                source="global",
                masked=mask_pixiv_token(row["refresh_token"]),
                is_default=False,
                owner_user_id=None,
                owner_username=None,
                created_at=row["created_at"],
            )
            for row in self._fetch_rows(owner_user_id=None, scope="global")
        ]
        if self.settings.pixiv_refresh_token:
            summaries.append(
                PixivTokenSummary(
                    id=pixiv_token_id_for(self.settings.pixiv_refresh_token),
                    source="system",
                    masked=mask_pixiv_token(self.settings.pixiv_refresh_token),
                    is_default=True,
                    owner_user_id=None,
                    owner_username=None,
                    created_at=None,
                )
            )
        return summaries

    def add_user_token(self, user: UserRecord, raw_token: str) -> None:
        self._add_token(raw_token, owner_user_id=user.id, scope="user")

    def add_global_token(self, raw_token: str) -> None:
        self._add_token(raw_token, owner_user_id=None, scope="global")

    def remove_user_token(self, user: UserRecord, token_id: str) -> None:
        with self.user_store._connect() as conn:
            self._ensure_hash_column(conn)
            conn.execute(
                "DELETE FROM pixiv_tokens WHERE owner_user_id = ? AND scope = 'user' AND refresh_token_hash = ?",
                (user.id, token_id),
            )
            conn.commit()

    def remove_global_token(self, token_id: str) -> None:
        with self.user_store._connect() as conn:
            self._ensure_hash_column(conn)
            conn.execute(
                "DELETE FROM pixiv_tokens WHERE owner_user_id IS NULL AND scope = 'global' AND refresh_token_hash = ?",
                (token_id,),
            )
            conn.commit()

    def _add_token(
        self, raw_token: str, *, owner_user_id: int | None, scope: str
    ) -> None:
        value = raw_token.strip()
        if not value:
            raise ValueError("Pixiv refresh token cannot be empty")
        token_id = pixiv_token_id_for(value)
        if self.settings.pixiv_refresh_token and token_id == pixiv_token_id_for(
            self.settings.pixiv_refresh_token
        ):
            return
        with self.user_store._connect() as conn:
            self._ensure_hash_column(conn)
            existing = conn.execute(
                "SELECT id FROM pixiv_tokens WHERE refresh_token_hash = ?",
                (token_id,),
            ).fetchone()
            if existing:
                return
            conn.execute(
                "INSERT INTO pixiv_tokens (owner_user_id, scope, refresh_token, refresh_token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (owner_user_id, scope, value, token_id, _utc_now()),
            )
            conn.commit()

    def _fetch_rows(
        self, *, owner_user_id: int | None, scope: str
    ) -> list[sqlite3.Row]:
        with self.user_store._connect() as conn:
            self._ensure_hash_column(conn)
            if owner_user_id is None:
                rows = conn.execute(
                    "SELECT * FROM pixiv_tokens WHERE owner_user_id IS NULL AND scope = ? ORDER BY id ASC",
                    (scope,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pixiv_tokens WHERE owner_user_id = ? AND scope = ? ORDER BY id ASC",
                    (owner_user_id, scope),
                ).fetchall()
        return rows

    @staticmethod
    def _ensure_hash_column(conn: sqlite3.Connection) -> None:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(pixiv_tokens)").fetchall()
        ]
        if "refresh_token_hash" not in columns:
            conn.execute("ALTER TABLE pixiv_tokens ADD COLUMN refresh_token_hash TEXT")
            rows = conn.execute("SELECT id, refresh_token FROM pixiv_tokens").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE pixiv_tokens SET refresh_token_hash = ? WHERE id = ?",
                    (pixiv_token_id_for(row["refresh_token"]), row["id"]),
                )
            conn.commit()
