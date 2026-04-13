from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from fanfictl.config import Settings
from fanfictl.models import Chapter, Work, WorkKind
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
            self.assertIn("English Title", response.text)

            response = client.get("/read/publictoken-english-title")
            self.assertEqual(response.status_code, 200)
            self.assertIn("English Title", response.text)


if __name__ == "__main__":
    unittest.main()
