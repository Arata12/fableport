from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from fanfictl.auth import UserStore
from fanfictl.config import Settings
from fanfictl.keystore import APIKeyStore
from fanfictl.models import Chapter, ExportFormat, Work, WorkKind
from fanfictl.pixiv_tokens import PixivTokenStore
from fanfictl.quota import QuotaTracker
from fanfictl.storage import ensure_work_dirs, save_metadata
from fanfictl.webapp import build_app


class WebTests(unittest.TestCase):
    def extract_csrf_token(self, html: str) -> str:
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
        self.assertIsNotNone(match)
        return str(match.group(1))

    def login(self, client: TestClient) -> None:
        response = client.get("/login")
        csrf_token = self.extract_csrf_token(response.text)
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "admin",
                "csrf_token": csrf_token,
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

    def dashboard_csrf_token(
        self, client: TestClient, path: str = "/dashboard/settings"
    ) -> str:
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        return self.extract_csrf_token(response.text)

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
                data={
                    "username": "admin",
                    "password": "admin",
                    "csrf_token": self.extract_csrf_token(client.get("/login").text),
                },
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("New import", response.text)
            self.assertIn("Gemma quota", response.text)

            response = client.get("/dashboard/library")
            self.assertEqual(response.status_code, 200)
            self.assertIn("English Title", response.text)
            self.assertIn("Retranslate", response.text)

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

    def test_reader_renders_embedded_images_from_local_assets(self) -> None:
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
                pixiv_id=790,
                source_url="https://example.com",
                public_id="imagetoken",
                original_title="原題",
                translated_title="Illustrated Title",
                author_name="Author",
                chapters=[
                    Chapter(
                        position=1,
                        pixiv_novel_id=790,
                        original_title="One",
                        translated_title="One",
                        source_markdown="# One\n\n![Pixiv embedded image 42](assets/embedded.jpg)",
                        translated_markdown="# One\n\n![Pixiv embedded image 42](assets/embedded.jpg)",
                    )
                ],
            )
            root = ensure_work_dirs(output_dir, work)
            save_metadata(root, work)
            assets_dir = root / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            (assets_dir / "embedded.jpg").write_bytes(b"fake-image")

            app = build_app(settings)
            client = TestClient(app)
            response = client.get("/read/imagetoken-illustrated-title")

            self.assertEqual(response.status_code, 200)
            self.assertIn('<img src="assets/embedded.jpg"', response.text)
            self.assertIn('<base href="/reader-assets/imagetoken/">', response.text)

            asset_response = client.get("/reader-assets/imagetoken/assets/embedded.jpg")
            self.assertEqual(asset_response.status_code, 200)
            self.assertEqual(asset_response.content, b"fake-image")

    def test_work_detail_can_queue_retranslation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            settings = Settings()
            settings.output_dir = output_dir
            settings.app_base_url = "http://localhost:8000"
            settings.app_secret_key = "test-secret"
            settings.admin_username = "admin"
            settings.admin_password = "admin"

            user_store = UserStore(settings)
            user = user_store.authenticate("admin", "admin")
            self.assertIsNotNone(user)

            work = Work(
                kind=WorkKind.NOVEL,
                pixiv_id=999,
                source_url="https://www.pixiv.net/novel/show.php?id=999",
                public_id="retranslatetoken",
                owner_user_id=user.id,
                owner_username=user.username,
                original_title="Original",
                translated_title="Translated",
                author_name="Author",
                chapters=[
                    Chapter(
                        position=1,
                        pixiv_novel_id=999,
                        original_title="One",
                        translated_title="One",
                        source_markdown="# One\n\nSource",
                        translated_markdown="# One\n\nTranslated",
                    )
                ],
            )
            root = ensure_work_dirs(output_dir, work)
            save_metadata(root, work)
            (root / "translated.md").write_text("# Translated\n", encoding="utf-8")
            (root / "translated.html").write_text(
                "<h1>Translated</h1>", encoding="utf-8"
            )

            app = build_app(settings)
            client = TestClient(app)
            self.login(client)

            detail_response = client.get(f"/works/{root.name}")
            self.assertEqual(detail_response.status_code, 200)
            self.assertIn("Retranslate", detail_response.text)
            csrf_token = self.extract_csrf_token(detail_response.text)

            with patch.object(
                app.state.jobs,
                "start_job",
                return_value=SimpleNamespace(id="job123"),
            ) as start_job:
                response = client.post(
                    f"/works/{root.name}/retranslate",
                    data={"csrf_token": csrf_token},
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/jobs/job123")
            start_job.assert_called_once()
            self.assertEqual(start_job.call_args.args[0], work.source_url)
            self.assertFalse(start_job.call_args.kwargs["resume"])
            self.assertEqual(
                start_job.call_args.kwargs["formats"],
                [ExportFormat.MD, ExportFormat.HTML],
            )

    def test_csrf_origin_check_uses_configured_public_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            settings = Settings()
            settings.output_dir = output_dir
            settings.app_base_url = "https://fableport.example"
            settings.app_secret_key = "test-secret"
            settings.admin_username = "admin"
            settings.admin_password = "admin"

            app = build_app(settings)
            client = TestClient(app, base_url="https://fableport.example")

            login_page = client.get("/login")
            csrf_token = self.extract_csrf_token(login_page.text)
            response = client.post(
                "/login",
                headers={"origin": "https://fableport.example"},
                data={
                    "username": "admin",
                    "password": "admin",
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )

            self.assertEqual(response.status_code, 303)

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
            self.login(client)
            csrf_token = self.dashboard_csrf_token(client)
            response = client.post(
                "/keys/personal",
                data={"api_key": "extra-fallback-key", "csrf_token": csrf_token},
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
            self.login(client)
            csrf_token = self.dashboard_csrf_token(client)
            response = client.post(
                "/pixiv/personal",
                data={
                    "refresh_token": "pixiv-refresh-token-demo",
                    "csrf_token": csrf_token,
                },
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
            self.login(client)
            csrf_token = self.dashboard_csrf_token(client)

            with patch(
                "fanfictl.webapp.create_oauth_session",
                return_value=("verifier123", "state123", "https://pixiv.example/login"),
            ):
                response = client.post(
                    "/pixiv/personal/oauth/start",
                    data={"csrf_token": csrf_token},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)

            with patch(
                "fanfictl.webapp.exchange_code_for_token",
                return_value={"refresh_token": "oauth-personal-token"},
            ):
                response = client.post(
                    "/pixiv/oauth/complete",
                    data={
                        "callback_input": "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?code=abc123&state=state123",
                        "csrf_token": csrf_token,
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

    def test_can_complete_personal_pixiv_oauth_from_post_redirect_url(self) -> None:
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
            self.login(client)
            csrf_token = self.dashboard_csrf_token(client)

            with patch(
                "fanfictl.webapp.create_oauth_session",
                return_value=("verifier123", "state123", "https://pixiv.example/login"),
            ):
                client.post(
                    "/pixiv/personal/oauth/start",
                    data={"csrf_token": csrf_token},
                    follow_redirects=False,
                )

            with patch(
                "fanfictl.webapp.exchange_code_for_token",
                return_value={"refresh_token": "oauth-post-redirect-token"},
            ) as exchange_mock:
                response = client.post(
                    "/pixiv/oauth/complete",
                    data={
                        "callback_input": "https://accounts.pixiv.net/post-redirect?return_to=https%3A%2F%2Fapp-api.pixiv.net%2Fweb%2Fv1%2Fusers%2Fauth%2Fpixiv%2Fcallback%3Fcode%3Dabc123%26state%3Dstate123",
                        "csrf_token": csrf_token,
                    },
                    follow_redirects=True,
                )

            self.assertEqual(response.status_code, 200)
            exchange_mock.assert_called_once()
            self.assertEqual(exchange_mock.call_args.kwargs["code"], "abc123")

    def test_intermediate_pixiv_redirect_shows_clear_error(self) -> None:
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
            self.login(client)
            csrf_token = self.dashboard_csrf_token(client)

            with patch(
                "fanfictl.webapp.create_oauth_session",
                return_value=("verifier123", "state123", "https://pixiv.example/login"),
            ):
                client.post(
                    "/pixiv/personal/oauth/start",
                    data={"csrf_token": csrf_token},
                    follow_redirects=False,
                )

            response = client.post(
                "/pixiv/oauth/complete",
                data={
                    "callback_input": "https://accounts.pixiv.net/post-redirect?return_to=https%253A%252F%252Fapp-api.pixiv.net%252Fweb%252Fv1%252Fusers%252Fauth%252Fpixiv%252Fstart%253F",
                    "csrf_token": csrf_token,
                },
                follow_redirects=True,
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("callback?...code=...", response.text)

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
            self.login(client)
            csrf_token = self.dashboard_csrf_token(client)
            response = client.post(
                "/account/password",
                data={
                    "current_password": "admin",
                    "new_password": "better-password",
                    "confirm_password": "better-password",
                    "csrf_token": csrf_token,
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
            self.login(client)
            csrf_token = self.dashboard_csrf_token(client, "/dashboard")
            response = client.post(
                "/submit",
                data={
                    "source_url": "https://www.pixiv.net/novel/show.php?id=27402134",
                    "csrf_token": csrf_token,
                },
            )

            self.assertEqual(response.status_code, 429)
            self.assertIn("Daily Gemini request limit reached", response.text)


if __name__ == "__main__":
    unittest.main()
