from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fanfictl.auth import UserRecord, UserStore, secret_id_for, utc_now
from fanfictl.config import Settings


def mask_secret(value: str, prefix_len: int = 6) -> str:
    if len(value) <= prefix_len + 4:
        return "*" * len(value)
    return f"{value[:prefix_len]}…{value[-4:]}"


key_id_for = secret_id_for


def mask_key(value: str) -> str:
    return mask_secret(value, 6)


@dataclass
class RuntimeAPIKey:
    id: str
    key: str
    source: str
    is_default: bool


@dataclass
class SecretSummary:
    id: str
    source: str
    masked: str
    is_default: bool
    created_at: str | None


APIKeySummary = SecretSummary


class _SecretStore:
    """Shared storage of user/global/env secrets, keyed by a hash of the value."""

    table: str
    value_column: str
    hash_column: str
    mask_prefix_len: int
    empty_error: str

    def __init__(self, settings: Settings, user_store: UserStore | None = None) -> None:
        self.settings = settings
        self.user_store = user_store or UserStore(settings)

    def _env_value(self) -> str:
        raise NotImplementedError

    def _runtime_entries(
        self, user: UserRecord | None
    ) -> list[tuple[str, str, str, bool]]:
        """Return (id, value, source, is_default), personal -> global -> env."""
        entries: list[tuple[str, str, str, bool]] = []
        seen: set[str] = set()

        def add(value: str, source: str, is_default: bool) -> None:
            secret_id = secret_id_for(value)
            if secret_id not in seen:
                seen.add(secret_id)
                entries.append((secret_id, value, source, is_default))

        if user:
            for row in self._fetch_rows(owner_user_id=user.id, scope="user"):
                add(row[self.value_column], "personal", False)
        for row in self._fetch_rows(owner_user_id=None, scope="global"):
            add(row[self.value_column], "global", False)
        env_value = self._env_value()
        if env_value:
            add(env_value, "system", True)
        return entries

    def _summaries(self, owner_user_id: int | None, scope: str, source: str):
        return [
            SecretSummary(
                id=secret_id_for(row[self.value_column]),
                source=source,
                masked=mask_secret(row[self.value_column], self.mask_prefix_len),
                is_default=False,
                created_at=row["created_at"],
            )
            for row in self._fetch_rows(owner_user_id=owner_user_id, scope=scope)
        ]

    def _personal_summaries(self, user: UserRecord) -> list[SecretSummary]:
        return self._summaries(user.id, "user", "personal")

    def _global_summaries(self) -> list[SecretSummary]:
        summaries = self._summaries(None, "global", "global")
        env_value = self._env_value()
        if env_value:
            summaries.append(
                SecretSummary(
                    id=secret_id_for(env_value),
                    source="system",
                    masked=mask_secret(env_value, self.mask_prefix_len),
                    is_default=True,
                    created_at=None,
                )
            )
        return summaries

    def _remove(self, secret_id: str, *, owner_user_id: int | None, scope: str) -> None:
        owner_sql = "owner_user_id IS NULL" if owner_user_id is None else "owner_user_id = ?"
        owner_params = () if owner_user_id is None else (owner_user_id,)
        with self.user_store._connect() as conn:
            conn.execute(
                f"DELETE FROM {self.table} WHERE {owner_sql} AND scope = ? AND {self.hash_column} = ?",
                (*owner_params, scope, secret_id),
            )
            conn.commit()

    def _add(self, raw: str, *, owner_user_id: int | None, scope: str) -> None:
        value = raw.strip()
        if not value:
            raise ValueError(self.empty_error)
        secret_id = secret_id_for(value)

        env_value = self._env_value()
        if env_value and secret_id == secret_id_for(env_value):
            return

        with self.user_store._connect() as conn:
            existing = conn.execute(
                f"SELECT id FROM {self.table} WHERE {self.hash_column} = ?",
                (secret_id,),
            ).fetchone()
            if existing:
                return
            conn.execute(
                f"INSERT INTO {self.table} (owner_user_id, scope, {self.value_column}, created_at, {self.hash_column}) VALUES (?, ?, ?, ?, ?)",
                (owner_user_id, scope, value, utc_now(), secret_id),
            )
            conn.commit()

    def _fetch_rows(
        self, *, owner_user_id: int | None, scope: str
    ) -> list[sqlite3.Row]:
        owner_sql = "owner_user_id IS NULL" if owner_user_id is None else "owner_user_id = ?"
        params = (scope,) if owner_user_id is None else (owner_user_id, scope)
        with self.user_store._connect() as conn:
            return conn.execute(
                f"SELECT * FROM {self.table} WHERE {owner_sql} AND scope = ? ORDER BY id ASC",
                params,
            ).fetchall()


class APIKeyStore(_SecretStore):
    table = "api_keys"
    value_column = "key_value"
    hash_column = "key_value_hash"
    mask_prefix_len = 6
    empty_error = "API key cannot be empty"

    def _env_value(self) -> str:
        return self.settings.ollama_api_key or ""

    def runtime_keys_for_user(self, user: UserRecord | None) -> list[RuntimeAPIKey]:
        return [
            RuntimeAPIKey(id=i, key=v, source=s, is_default=d)
            for i, v, s, d in self._runtime_entries(user)
        ]

    def list_personal_keys(self, user: UserRecord) -> list[SecretSummary]:
        return self._personal_summaries(user)

    def list_global_keys(self) -> list[SecretSummary]:
        return self._global_summaries()

    def add_user_key(self, user: UserRecord, raw_key: str) -> None:
        self._add(raw_key, owner_user_id=user.id, scope="user")

    def add_global_key(self, raw_key: str) -> None:
        self._add(raw_key, owner_user_id=None, scope="global")

    def remove_user_key(self, user: UserRecord, key_id: str) -> None:
        self._remove(key_id, owner_user_id=user.id, scope="user")

    def remove_global_key(self, key_id: str) -> None:
        self._remove(key_id, owner_user_id=None, scope="global")
