from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from fanfictl.auth import UserStore
from fanfictl.config import Settings
from fanfictl.keystore import APIKeyStore
from fanfictl.models import Chapter, Work, WorkKind
from fanfictl.pixiv_tokens import PixivTokenStore
from fanfictl.quota import QuotaTracker
from fanfictl.storage import ensure_work_dirs, save_metadata
from fanfictl.webapp import build_app


class WebTests(unittest.TestCase):
    def test_login_dashboard_and_public_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            settings = Settings()
            settings.output_dir = output_dir
            settings.app_base_url = "http://localhost:8000"
            settings.app_secret_key = "test-secret"
            settings.admin_username = "admin"
            settings.admin_password = "admin"

            work = Work(
                kind=WorkKind.NOVEL,
                pixiv_id=456,
                source_url="https://example.com",
                public_id="publictoken",
                original_title="原題",
                translated_title="English Title",
                author_name="Author",
                chapters=[
                    Chapter(
                        position=1,
                        pixiv_novel_id=456,
                        original_title="One",
                        translated_title="One",
                        source_markdown="# One\n\nSource",
                        translated_markdown="# One\n\nTranslated",
                    )
                ],
            )
            root = ensure_work_dirs(output_dir, work)
            save_metadata(root, work)
            (root / "translated.md").write_text(
                "# English Title\n\nTranslated", encoding="utf-8"
            )

            app = build_app(settings)
            client = TestClient(app)

            response = client.get("/dashboard", follow_redirects=False)
            self.assertEqual(response.status_code, 303)

            response = client.post(
                "/login",
                data={"username": "admin", "password": "admin"},
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("New import", response.text)
            self.assertIn("Gemma quota", response.text)

            response = client.get("/dashboard/library")
            self.assertEqual(response.status_code, 200)
            self.assertIn("English Title", response.text)

            response = client.get("/read/publictoken-english-title")
            self.assertEqual(response.status_code, 200)
            self.assertIn("English Title", response.text)

    def test_reader_preserves_single_line_breaks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            settings = Settings()
            settings.output_dir = output_dir
            settings.app_base_url = "http://localhost:8000"
            settings.app_secret_key = "test-secret"
            settings.admin_username = "admin"
            settings.admin_password = "admin"

            work = Work(
                kind=WorkKind.NOVEL,
                pixiv_id=789,
                source_url="https://example.com",
                public_id="breaktoken",
                original_title="原題",
                translated_title="Readable Title",
                author_name="Author",
                chapters=[
                    Chapter(
                        position=1,
                        pixiv_novel_id=789,
                        original_title="One",
                        translated_title="One",
                        source_markdown="# One\n\n[first line]\n[second line]",
                        translated_markdown="# One\n\n[first line]\n[second line]",
                    )
                ],
            )
            root = ensure_work_dirs(output_dir, work)
            save_metadata(root, work)
            (root / "translated.md").write_text(
                "# Readable Title\n\n[first line]\n[second line]", encoding="utf-8"
            )

            app = build_app(settings)
            client = TestClient(app)
            response = client.get("/read/breaktoken-readable-title")

            self.assertEqual(response.status_code, 200)
            self.assertIn("<br>", response.text)

    def test_can_add_fallback_key_from_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            settings = Settings()
            settings.output_dir = output_dir
            settings.app_base_url = "http://localhost:8000"
            settings.app_secret_key = "test-secret"
            settings.admin_username = "admin"
            settings.admin_password = "admin"
            settings.gemini_api_key = "env-primary-key"

            app = build_app(settings)
            client = TestClient(app)
            client.post("/login", data={"username": "admin", "password": "admin"})
            response = client.post(
                "/keys/personal",
                data={"api_key": "extra-fallback-key"},
                follow_redirects=True,
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("Personal API keys", response.text)
            user_store = UserStore(settings)
            user = user_store.authenticate("admin", "admin")
            self.assertIsNotNone(user)
            self.assertEqual(
                len(APIKeyStore(settings, user_store).runtime_keys_for_user(user)), 2
            )

    def test_can_add_personal_pixiv_token_from_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            settings = Settings()
            settings.output_dir = output_dir
            settings.app_base_url = "http://localhost:8000"
            settings.app_secret_key = "test-secret"
            settings.admin_username = "admin"
            settings.admin_password = "admin"

            app = build_app(settings)
            client = TestClient(app)
            client.post("/login", data={"username": "admin", "password": "admin"})
            response = client.post(
                "/pixiv/personal",
                data={"refresh_token": "pixiv-refresh-token-demo"},
                follow_redirects=True,
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("Personal Pixiv token", response.text)
            user_store = UserStore(settings)
            user = user_store.authenticate("admin", "admin")
            self.assertIsNotNone(user)
            self.assertEqual(
                len(
                    PixivTokenStore(settings, user_store).runtime_tokens_for_user(user)
                ),
                1,
            )

    def test_can_complete_personal_pixiv_oauth_from_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            settings = Settings()
            settings.output_dir = output_dir
            settings.app_base_url = "http://localhost:8000"
            settings.app_secret_key = "test-secret"
            settings.admin_username = "admin"
            settings.admin_password = "admin"

            app = build_app(settings)
            client = TestClient(app)
            client.post("/login", data={"username": "admin", "password": "admin"})

            with patch(
                "fanfictl.webapp.create_oauth_session",
                return_value=("verifier123", "state123", "https://pixiv.example/login"),
            ):
                response = client.post(
                    "/pixiv/personal/oauth/start", follow_redirects=False
                )
                self.assertEqual(response.status_code, 303)

            with patch(
                "fanfictl.webapp.exchange_code_for_token",
                return_value={"refresh_token": "oauth-personal-token"},
            ):
                response = client.post(
                    "/pixiv/oauth/complete",
                    data={
                        "callback_input": "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?code=abc123&state=state123"
                    },
                    follow_redirects=True,
                )

            self.assertEqual(response.status_code, 200)
            user_store = UserStore(settings)
            user = user_store.authenticate("admin", "admin")
            self.assertIsNotNone(user)
            self.assertEqual(
                len(
                    PixivTokenStore(settings, user_store).runtime_tokens_for_user(user)
                ),
                1,
            )

    def test_user_can_change_password_in_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            settings = Settings()
            settings.output_dir = output_dir
            settings.app_base_url = "http://localhost:8000"
            settings.app_secret_key = "test-secret"
            settings.admin_username = "admin"
            settings.admin_password = "admin"

            app = build_app(settings)
            client = TestClient(app)
            client.post("/login", data={"username": "admin", "password": "admin"})
            response = client.post(
                "/account/password",
                data={
                    "current_password": "admin",
                    "new_password": "better-password",
                    "confirm_password": "better-password",
                },
                follow_redirects=True,
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("Account", response.text)
            self.assertIsNotNone(
                UserStore(settings).authenticate("admin", "better-password")
            )

    def test_submit_blocked_when_daily_quota_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            settings = Settings()
            settings.output_dir = output_dir
            settings.app_base_url = "http://localhost:8000"
            settings.app_secret_key = "test-secret"
            settings.admin_username = "admin"
            settings.admin_password = "admin"
            settings.gemini_rpm_limit = settings.gemini_rpd_limit + 1
            settings.gemini_api_key = "env-primary-key"

            user_store = UserStore(settings)
            user = user_store.authenticate("admin", "admin")
            self.assertIsNotNone(user)
            tracker = QuotaTracker(
                settings, APIKeyStore(settings, user_store).runtime_keys_for_user(user)
            )
            for _ in range(settings.gemini_rpd_limit):
                tracker.acquire_request_slot()

            app = build_app(settings)
            client = TestClient(app)
            client.post("/login", data={"username": "admin", "password": "admin"})
            response = client.post(
                "/submit",
                data={"source_url": "https://www.pixiv.net/novel/show.php?id=27402134"},
            )

            self.assertEqual(response.status_code, 429)
            self.assertIn("Daily Gemini request limit reached", response.text)


if __name__ == "__main__":
    unittest.main()
