from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from fanfictl.auth import UserRecord, UserStore
from fanfictl.config import Settings


def key_id_for(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def mask_key(value: str) -> str:
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:6]}…{value[-4:]}"


@dataclass
class RuntimeAPIKey:
    id: str
    key: str
    source: str
    is_default: bool
    owner_user_id: int | None = None
    owner_username: str | None = None


@dataclass
class APIKeySummary:
    id: str
    source: str
    masked: str
    is_default: bool
    owner_user_id: int | None
    owner_username: str | None
    created_at: str | None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class APIKeyStore:
    def __init__(self, settings: Settings, user_store: UserStore | None = None) -> None:
        self.settings = settings
        self.user_store = user_store or UserStore(settings)

    def runtime_keys_for_user(self, user: UserRecord | None) -> list[RuntimeAPIKey]:
        keys: list[RuntimeAPIKey] = []
        seen: set[str] = set()

        if user:
            for row in self._fetch_rows(owner_user_id=user.id, scope="user"):
                key_id = key_id_for(row["key_value"])
                if key_id in seen:
                    continue
                keys.append(
                    RuntimeAPIKey(
                        id=key_id,
                        key=row["key_value"],
                        source="personal",
                        is_default=False,
                        owner_user_id=user.id,
                        owner_username=user.username,
                    )
                )
                seen.add(key_id)

        for row in self._fetch_rows(owner_user_id=None, scope="global"):
            key_id = key_id_for(row["key_value"])
            if key_id in seen:
                continue
            keys.append(
                RuntimeAPIKey(
                    id=key_id,
                    key=row["key_value"],
                    source="global",
                    is_default=False,
                    owner_user_id=None,
                    owner_username=None,
                )
            )
            seen.add(key_id)

        if self.settings.gemini_api_key:
            key_id = key_id_for(self.settings.gemini_api_key)
            if key_id not in seen:
                keys.append(
                    RuntimeAPIKey(
                        id=key_id,
                        key=self.settings.gemini_api_key,
                        source="system",
                        is_default=True,
                        owner_user_id=None,
                        owner_username=None,
                    )
                )
        return keys

    def list_personal_keys(self, user: UserRecord) -> list[APIKeySummary]:
        return [
            APIKeySummary(
                id=key_id_for(row["key_value"]),
                source="personal",
                masked=mask_key(row["key_value"]),
                is_default=False,
                owner_user_id=user.id,
                owner_username=user.username,
                created_at=row["created_at"],
            )
            for row in self._fetch_rows(owner_user_id=user.id, scope="user")
        ]

    def list_global_keys(self) -> list[APIKeySummary]:
        summaries = [
            APIKeySummary(
                id=key_id_for(row["key_value"]),
                source="global",
                masked=mask_key(row["key_value"]),
                is_default=False,
                owner_user_id=None,
                owner_username=None,
                created_at=row["created_at"],
            )
            for row in self._fetch_rows(owner_user_id=None, scope="global")
        ]
        if self.settings.gemini_api_key:
            summaries.append(
                APIKeySummary(
                    id=key_id_for(self.settings.gemini_api_key),
                    source="system",
                    masked=mask_key(self.settings.gemini_api_key),
                    is_default=True,
                    owner_user_id=None,
                    owner_username=None,
                    created_at=None,
                )
            )
        return summaries

    def add_user_key(self, user: UserRecord, raw_key: str) -> None:
        self._add_key(raw_key, owner_user_id=user.id, scope="user")

    def add_global_key(self, raw_key: str) -> None:
        self._add_key(raw_key, owner_user_id=None, scope="global")

    def remove_user_key(self, user: UserRecord, key_id: str) -> None:
        with self.user_store._connect() as conn:
            self._ensure_hash_column(conn)
            conn.execute(
                "DELETE FROM api_keys WHERE owner_user_id = ? AND scope = 'user' AND key_value_hash = ?",
                (user.id, key_id),
            )
            conn.commit()

    def remove_global_key(self, key_id: str) -> None:
        with self.user_store._connect() as conn:
            self._ensure_hash_column(conn)
            conn.execute(
                "DELETE FROM api_keys WHERE owner_user_id IS NULL AND scope = 'global' AND key_value_hash = ?",
                (key_id,),
            )
            conn.commit()

    def _add_key(self, raw_key: str, *, owner_user_id: int | None, scope: str) -> None:
        value = raw_key.strip()
        if not value:
            raise ValueError("API key cannot be empty")
        key_id = key_id_for(value)

        if self.settings.gemini_api_key and key_id == key_id_for(
            self.settings.gemini_api_key
        ):
            return

        with self.user_store._connect() as conn:
            self._ensure_hash_column(conn)
            existing = conn.execute(
                "SELECT id FROM api_keys WHERE key_value_hash = ?",
                (key_id,),
            ).fetchone()
            if existing:
                return
            conn.execute(
                "INSERT INTO api_keys (owner_user_id, scope, key_value, created_at, key_value_hash) VALUES (?, ?, ?, ?, ?)",
                (owner_user_id, scope, value, _utc_now(), key_id),
            )
            conn.commit()

    def _fetch_rows(
        self, *, owner_user_id: int | None, scope: str
    ) -> list[sqlite3.Row]:
        with self.user_store._connect() as conn:
            self._ensure_hash_column(conn)
            if owner_user_id is None:
                rows = conn.execute(
                    "SELECT * FROM api_keys WHERE owner_user_id IS NULL AND scope = ? ORDER BY id ASC",
                    (scope,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM api_keys WHERE owner_user_id = ? AND scope = ? ORDER BY id ASC",
                    (owner_user_id, scope),
                ).fetchall()
        return rows

    @staticmethod
    def _ensure_hash_column(conn: sqlite3.Connection) -> None:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(api_keys)").fetchall()
        ]
        if "key_value_hash" not in columns:
            conn.execute("ALTER TABLE api_keys ADD COLUMN key_value_hash TEXT")
            rows = conn.execute("SELECT id, key_value FROM api_keys").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE api_keys SET key_value_hash = ? WHERE id = ?",
                    (key_id_for(row["key_value"]), row["id"]),
                )
            conn.commit()
