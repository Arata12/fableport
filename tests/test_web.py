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
            settings.require_login = True

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
            self.assertIn("Usage", response.text)

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
                "start_translate_job",
                return_value=SimpleNamespace(id="job123"),
            ) as start_translate_job:
                settings.ollama_api_key = "test-key"
                response = client.post(
                    f"/works/{root.name}/retranslate",
                    data={"csrf_token": csrf_token},
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/jobs/job123")
            start_translate_job.assert_called_once()
            self.assertEqual(start_translate_job.call_args.args[0], root.name)
            self.assertFalse(start_translate_job.call_args.kwargs["resume"])
            self.assertEqual(
                start_translate_job.call_args.kwargs["formats"],
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
            settings.ollama_api_key = "env-primary-key"

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
            settings.require_login = True

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
            settings.rpd_limit = 3
            settings.rpm_limit = 0
            settings.ollama_api_key = "env-primary-key"

            user_store = UserStore(settings)
            user = user_store.authenticate("admin", "admin")
            self.assertIsNotNone(user)
            tracker = QuotaTracker(
                settings, APIKeyStore(settings, user_store).runtime_keys_for_user(user)
            )
            for _ in range(settings.rpd_limit):
                tracker.acquire_request_slot()

            app = build_app(settings)
            client = TestClient(app)
            self.login(client)
            csrf_token = self.dashboard_csrf_token(client, "/dashboard")
            response = client.post(
                "/submit",
                data={
                    "source_urls": "https://www.pixiv.net/novel/show.php?id=27402134",
                    "action": "translate",
                    "csrf_token": csrf_token,
                },
            )

            self.assertEqual(response.status_code, 429)
            self.assertIn("Daily request limit reached", response.text)

    def make_settings(self, tmp: str) -> Settings:
        settings = Settings()
        settings.output_dir = Path(tmp) / "output"
        settings.app_base_url = "http://localhost:8000"
        settings.app_secret_key = "test-secret"
        settings.admin_username = "admin"
        settings.admin_password = "admin"
        return settings

    def test_local_mode_needs_no_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.assertFalse(settings.require_login)
            client = TestClient(build_app(settings))
            response = client.get("/", follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Local mode", response.text)
            self.assertNotIn("Log out", response.text)
            settings_page = client.get("/dashboard/settings").text
            self.assertIn("Translation engine", settings_page)
            self.assertNotIn("Change password", settings_page)
            self.assertEqual(client.get("/login", follow_redirects=False).status_code, 303)

            # Forms work when the browser reaches the app through a Tailscale IP.
            token = self.dashboard_csrf_token(client)
            response = client.post(
                "/keys/personal",
                data={"api_key": "local-key", "csrf_token": token},
                headers={
                    "host": "100.101.102.103:8000",
                    "origin": "http://100.101.102.103:8000",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            response = client.post(
                "/keys/personal",
                data={"api_key": "x", "csrf_token": token},
                headers={"origin": "https://evil.example"},
            )
            self.assertEqual(response.status_code, 403)

    def test_admin_edits_translation_settings_in_web(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            client = TestClient(build_app(settings))
            self.login(client)
            form = {
                "ollama_model": "qwen3:235b",
                "ollama_host": "https://ollama.com/",
                "translation_concurrency": "5",
                "rpm_limit": "0",
                "rpd_limit": "200",
                "csrf_token": self.dashboard_csrf_token(client),
            }
            response = client.post("/settings/translation", data=form, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('value="qwen3:235b"', response.text)
            self.assertEqual(settings.ollama_model, "qwen3:235b")
            self.assertEqual(settings.ollama_host, "https://ollama.com")
            self.assertEqual(settings.translation_concurrency, 5)
            self.assertEqual(settings.rpd_limit, 200)

            # Survives a restart: a fresh Settings picks the stored values up.
            fresh = self.make_settings(tmp)
            UserStore(fresh)
            self.assertEqual(fresh.ollama_model, "qwen3:235b")
            self.assertEqual(fresh.translation_concurrency, 5)

            form["ollama_host"] = "ollama.com"
            response = client.post("/settings/translation", data=form)
            self.assertEqual(response.status_code, 400)
            self.assertIn("Host must start with", response.text)

    def test_session_secret_is_generated_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = self.make_settings(tmp)
            first.app_secret_key = ""
            build_app(first)
            second = self.make_settings(tmp)
            second.app_secret_key = ""
            build_app(second)
            self.assertTrue(first.app_secret_key)
            self.assertEqual(first.app_secret_key, second.app_secret_key)

    def test_submit_rejects_unsupported_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            settings.ollama_api_key = "env-primary-key"
            client = TestClient(build_app(settings))
            self.login(client)
            csrf_token = self.dashboard_csrf_token(client, "/dashboard")
            response = client.post(
                "/submit",
                data={"source_url": " https://example.com/nope ", "csrf_token": csrf_token},
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Unsupported Pixiv URL", response.text)

    def test_submit_without_api_keys_shows_add_key_hint_when_translating_cloud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            settings.ollama_api_key = ""
            settings.ollama_host = "https://ollama.com"
            client = TestClient(build_app(settings))
            self.login(client)
            csrf_token = self.dashboard_csrf_token(client, "/dashboard")
            response = client.post(
                "/submit",
                data={
                    "source_urls": "https://www.pixiv.net/novel/show.php?id=27402134",
                    "action": "translate",
                    "csrf_token": csrf_token,
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Add an Ollama API key in Settings", response.text)
            self.assertNotIn("Daily request limit reached", response.text)

    def test_import_only_works_without_api_key_and_supports_batch_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            settings.ollama_api_key = ""
            app = build_app(settings)
            client = TestClient(app)
            csrf_token = self.dashboard_csrf_token(client, "/dashboard")

            with patch.object(
                app.state.jobs,
                "start_import_job",
                side_effect=[SimpleNamespace(id="imp1"), SimpleNamespace(id="imp2")],
            ) as start_import:
                response = client.post(
                    "/submit",
                    data={
                        "source_urls": (
                            "https://www.pixiv.net/novel/show.php?id=111\n"
                            "https://www.pixiv.net/novel/series/222\n"
                        ),
                        "action": "import",
                        "csrf_token": csrf_token,
                    },
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/dashboard")
            self.assertEqual(start_import.call_count, 2)

    def test_translate_all_pending_works_without_key_on_local_openai(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            settings.ollama_api_key = ""
            settings.api_mode = "openai"
            settings.ollama_host = "http://127.0.0.1:8001"
            settings.target_language = "Spanish"

            user_store = UserStore(settings)
            admin = user_store.get_admin()
            self.assertIsNotNone(admin)

            pending_work = Work(
                kind=WorkKind.NOVEL,
                pixiv_id=501,
                source_url="https://www.pixiv.net/novel/show.php?id=501",
                public_id="pendingtok",
                owner_user_id=admin.id,
                owner_username=admin.username,
                original_title="未翻訳",
                author_name="Author",
                chapters=[
                    Chapter(
                        position=1,
                        pixiv_novel_id=501,
                        original_title="Ch 1",
                        source_markdown="# Ch 1\n\nTexto",
                    )
                ],
            )
            root = ensure_work_dirs(settings.output_dir, pending_work)
            save_metadata(root, pending_work)

            app = build_app(settings)
            client = TestClient(app)
            lib_page = client.get("/dashboard/library")
            self.assertEqual(lib_page.status_code, 200)
            self.assertIn("Untranslated", lib_page.text)
            self.assertIn("Translate all pending (1)", lib_page.text)
            csrf = self.extract_csrf_token(lib_page.text)

            with patch.object(
                app.state.jobs,
                "start_translate_job",
                return_value=SimpleNamespace(id="tr1"),
            ) as start_tr:
                resp = client.post(
                    "/works/translate-all",
                    data={"csrf_token": csrf, "scope": "mine"},
                    follow_redirects=False,
                )
            self.assertEqual(resp.status_code, 303)
            start_tr.assert_called_once()
            self.assertEqual(start_tr.call_args.args[0], root.name)
            self.assertEqual(start_tr.call_args.kwargs["target_language"], "Spanish")
            self.assertTrue(start_tr.call_args.kwargs["resume"])

    def test_admin_edits_api_mode_target_language_and_colab_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            app = build_app(settings)
            client = TestClient(app)
            csrf = self.dashboard_csrf_token(client)

            resp = client.post(
                "/settings/translation",
                data={
                    "api_mode": "openai",
                    "target_language": "Spanish",
                    "ollama_model": "Qwen/Qwen2.5-14B-Instruct-AWQ",
                    "ollama_host": "http://host.docker.internal:8001/",
                    "translation_concurrency": "6",
                    "rpm_limit": "0",
                    "rpd_limit": "0",
                    "csrf_token": csrf,
                },
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(settings.api_mode, "openai")
            self.assertEqual(settings.target_language, "Spanish")
            self.assertEqual(settings.ollama_host, "http://host.docker.internal:8001")

            csrf = self.extract_csrf_token(resp.text)
            resp2 = client.post(
                "/settings/colab",
                data={
                    "colab_enabled": "1",
                    "colab_gpu": "A100",
                    "colab_high_mem": "1",
                    "colab_engine": "vllm",
                    "colab_model": "google/gemma-4-31b-it",
                    "colab_extra_args": "--max-model-len 16384",
                    "colab_hf_token": "hf_secret_demo",
                    "colab_auto_start": "1",
                    "colab_auto_stop": "1",
                    "colab_idle_stop_minutes": "5",
                    "colab_session_name": "fableport-a100",
                    "csrf_token": csrf,
                },
                follow_redirects=True,
            )
            self.assertEqual(resp2.status_code, 200)
            self.assertTrue(settings.colab_enabled)
            self.assertEqual(settings.colab_gpu, "A100")
            self.assertTrue(settings.colab_high_mem)
            self.assertEqual(settings.colab_model, "google/gemma-4-31b-it")
            self.assertEqual(settings.colab_hf_token, "hf_secret_demo")
            self.assertEqual(settings.colab_idle_stop_minutes, 5)
            # Never leak HF token in rendered HTML.
            self.assertNotIn("hf_secret_demo", resp2.text)

            fresh = self.make_settings(tmp)
            UserStore(fresh)
            self.assertEqual(fresh.api_mode, "openai")
            self.assertEqual(fresh.target_language, "Spanish")
            self.assertTrue(fresh.colab_enabled)
            self.assertEqual(fresh.colab_gpu, "A100")
            self.assertEqual(fresh.colab_hf_token, "hf_secret_demo")

    def test_colab_oauth_and_adc_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            app = build_app(settings)
            client = TestClient(app)
            csrf = self.dashboard_csrf_token(client)

            with patch(
                "fanfictl.webapp.create_colab_oauth_session",
                return_value=("ver123", "st123", "https://accounts.google.com/mock"),
            ):
                start_resp = client.post(
                    "/colab/oauth/start",
                    data={"csrf_token": csrf},
                    follow_redirects=False,
                )
                self.assertEqual(start_resp.status_code, 303)
                self.assertEqual(
                    start_resp.headers["location"], "https://accounts.google.com/mock"
                )

            with patch(
                "fanfictl.webapp.exchange_colab_code",
                return_value={
                    "type": "authorized_user",
                    "client_id": "12345-cid.apps.googleusercontent.com",
                    "client_secret": "csec",
                    "refresh_token": "1//refresh-demo",
                },
            ):
                done_resp = client.post(
                    "/colab/oauth/complete",
                    data={"auth_code": "4/0A-demo-code", "csrf_token": csrf},
                    follow_redirects=True,
                )
                self.assertEqual(done_resp.status_code, 200)
                self.assertIn("Google Colab account connected", done_resp.text)
                self.assertTrue(app.state.colab.has_adc_credentials())

            csrf = self.extract_csrf_token(done_resp.text)
            disc_resp = client.post(
                "/colab/disconnect",
                data={"csrf_token": csrf},
                follow_redirects=True,
            )
            self.assertEqual(disc_resp.status_code, 200)
            self.assertFalse(app.state.colab.has_adc_credentials())

    def test_unified_provider_settings_and_context_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            app = build_app(settings)
            client = TestClient(app)
            csrf = self.dashboard_csrf_token(client)

            resp = client.post(
                "/settings/translation",
                data={
                    "provider_backend": "colab",
                    "target_language": "Spanish",
                    "translation_concurrency": "4",
                    "context_length": "65536",
                    "colab_form_present": "1",
                    "colab_gpu": "A100",
                    "colab_engine": "ollama",
                    "colab_model": "gemma4:31b",
                    "colab_auto_start": "1",
                    "colab_auto_stop": "1",
                    "colab_session_name": "fableport",
                    "colab_idle_stop_minutes": "0",
                    "csrf_token": csrf,
                },
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(settings.provider_backend, "colab")
            self.assertTrue(settings.colab_enabled)
            self.assertEqual(settings.context_length, 65536)
            self.assertEqual(settings.colab_engine, "ollama")
            self.assertEqual(settings.colab_gpu, "A100")

            # Switch to ollama_cloud in the same unified form.
            csrf = self.extract_csrf_token(resp.text)
            resp2 = client.post(
                "/settings/translation",
                data={
                    "provider_backend": "ollama_cloud",
                    "target_language": "English",
                    "translation_concurrency": "3",
                    "context_length": "0",
                    "cloud_model": "gemma4:31b",
                    "csrf_token": csrf,
                },
                follow_redirects=True,
            )
            self.assertEqual(resp2.status_code, 200)
            self.assertEqual(settings.provider_backend, "ollama_cloud")
            self.assertFalse(settings.colab_enabled)
            self.assertEqual(settings.ollama_host, "https://ollama.com")

    def test_delete_work_from_library_and_pixiv_console_code_extraction(self) -> None:
        from fanfictl.pixiv_oauth import extract_code

        self.assertEqual(
            extract_code(
                "Failed to launch 'pixiv://account/login?code=abcXYZ99&via=login' because the scheme does not have a registered handler."
            ),
            "abcXYZ99",
        )
        self.assertEqual(
            extract_code("pixiv://account/login?code=onlycode123&via=login"),
            "onlycode123",
        )

        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            user_store = UserStore(settings)
            admin = user_store.get_admin()
            self.assertIsNotNone(admin)

            work = Work(
                kind=WorkKind.NOVEL,
                pixiv_id=888,
                source_url="https://www.pixiv.net/novel/show.php?id=888",
                public_id="deltoken888",
                owner_user_id=admin.id,
                owner_username=admin.username,
                original_title="Delete Me",
                author_name="Author",
                chapters=[
                    Chapter(
                        position=1,
                        pixiv_novel_id=888,
                        original_title="One",
                        source_markdown="# One\n\nBody",
                    )
                ],
            )
            root = ensure_work_dirs(settings.output_dir, work)
            save_metadata(root, work)

            app = build_app(settings)
            client = TestClient(app)
            lib = client.get("/dashboard/library")
            self.assertIn("Delete Me", lib.text)
            csrf = self.extract_csrf_token(lib.text)

            del_resp = client.post(
                f"/works/{root.name}/delete",
                data={"csrf_token": csrf},
                follow_redirects=True,
            )
            self.assertEqual(del_resp.status_code, 200)
            self.assertFalse(root.exists())
            self.assertIn("Deleted &#39;Delete Me&#39;.", del_resp.text)
            self.assertEqual(client.get("/read/deltoken888-delete-me").status_code, 404)

    def test_reader_assets_only_serve_assets_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            work = Work(
                kind=WorkKind.NOVEL,
                pixiv_id=791,
                source_url="https://example.com",
                public_id="assettoken",
                original_title="原題",
                translated_title="Asset Title",
                author_name="Author",
                chapters=[],
            )
            root = ensure_work_dirs(settings.output_dir, work)
            save_metadata(root, work)
            (root / "assets").mkdir(parents=True, exist_ok=True)
            (root / "assets" / "a.jpg").write_bytes(b"img")

            client = TestClient(build_app(settings))
            self.assertEqual(
                client.get("/reader-assets/assettoken/assets/a.jpg").status_code, 200
            )
            self.assertEqual(
                client.get("/reader-assets/assettoken/metadata.json").status_code, 404
            )
            self.assertEqual(
                client.get("/reader-assets/assettoken/assets/../metadata.json").status_code,
                404,
            )


if __name__ == "__main__":
    unittest.main()
