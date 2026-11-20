from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fanfictl.auth import UserStore, secret_id_for
from fanfictl.config import Settings
from fanfictl.keystore import APIKeyStore
from fanfictl.pixiv_tokens import PixivTokenStore


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_schema_is_migrated_and_removable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.output_dir.mkdir(parents=True)
            conn = sqlite3.connect(settings.output_dir / "app.db")
            conn.execute(
                "CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER NULL, scope TEXT NOT NULL, key_value TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE pixiv_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER NULL, scope TEXT NOT NULL, refresh_token TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO api_keys (owner_user_id, scope, key_value, created_at) VALUES (NULL, 'global', 'legacy-gemini-key-123', 'x')"
            )
            conn.execute(
                "INSERT INTO pixiv_tokens (owner_user_id, scope, refresh_token, created_at) VALUES (NULL, 'global', 'legacy-pixiv-token-123', 'x')"
            )
            conn.commit()
            conn.close()

            users = UserStore(settings)
            keys = APIKeyStore(settings, users)
            tokens = PixivTokenStore(settings, users)

            key_id = secret_id_for("legacy-gemini-key-123")
            self.assertEqual([k.id for k in keys.list_global_keys()], [key_id])
            keys.remove_global_key(key_id)
            self.assertEqual(keys.list_global_keys(), [])

            token_id = secret_id_for("legacy-pixiv-token-123")
            self.assertEqual([t.id for t in tokens.list_global_tokens()], [token_id])
            tokens.remove_global_token(token_id)
            self.assertEqual(tokens.list_global_tokens(), [])


if __name__ == "__main__":
    unittest.main()
