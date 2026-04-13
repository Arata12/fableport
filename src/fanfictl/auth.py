from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fanfictl.config import Settings


@dataclass
class UserRecord:
    id: int
    username: str
    role: str
    created_at: str
    is_active: bool


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    iterations = 600_000
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iter_str, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        digest = base64.b64decode(digest_b64.encode("ascii"))
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iter_str),
        )
        return hmac.compare_digest(candidate, digest)
    except Exception:  # noqa: BLE001
        return False


class UserStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = settings.output_dir / "app.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.bootstrap_admin()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                  created_at TEXT NOT NULL,
                  is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  owner_user_id INTEGER NULL,
                  scope TEXT NOT NULL CHECK(scope IN ('global', 'user')),
                  key_value TEXT NOT NULL,
                  key_value_hash TEXT,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.commit()

    def bootstrap_admin(self) -> None:
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            existing = conn.execute(
                "SELECT id FROM users WHERE username = ?",
                (self.settings.admin_username,),
            ).fetchone()
            if count and existing:
                return
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at, is_active) VALUES (?, ?, 'admin', ?, 1)",
                (
                    self.settings.admin_username,
                    hash_password(self.settings.admin_password),
                    _utc_now(),
                ),
            )
            conn.commit()

    def authenticate(self, username: str, password: str) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? AND is_active = 1",
                (username,),
            ).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            return None
        return self._row_to_user(row)

    def get_user(self, user_id: int) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self) -> list[UserRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY role DESC, username ASC"
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def create_user(
        self, username: str, password: str, role: str = "user"
    ) -> UserRecord:
        username = username.strip()
        if not username:
            raise ValueError("Username cannot be empty")
        if not password:
            raise ValueError("Password cannot be empty")
        if role not in {"admin", "user"}:
            raise ValueError("Invalid user role")

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
            if existing:
                raise ValueError("Username already exists")
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at, is_active) VALUES (?, ?, ?, ?, 1)",
                (username, hash_password(password), role, _utc_now()),
            )
            user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        user = self.get_user(int(user_id))
        if not user:
            raise RuntimeError("Failed to create user")
        return user

    def change_password(
        self, user: UserRecord, current_password: str, new_password: str
    ) -> None:
        if not new_password:
            raise ValueError("New password cannot be empty")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = ? AND is_active = 1",
                (user.id,),
            ).fetchone()
            if not row or not verify_password(current_password, row["password_hash"]):
                raise ValueError("Current password is incorrect")
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(new_password), user.id),
            )
            conn.commit()

    def _row_to_user(self, row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            id=int(row["id"]),
            username=str(row["username"]),
            role=str(row["role"]),
            created_at=str(row["created_at"]),
            is_active=bool(row["is_active"]),
        )
