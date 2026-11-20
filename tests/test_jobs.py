from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fanfictl.auth import UserStore
from fanfictl.config import Settings
from fanfictl.jobs import JobManager
from fanfictl.keystore import APIKeyStore
from fanfictl.models import Work, WorkKind
from fanfictl.storage import ensure_work_dirs


class JobQueueTests(unittest.TestCase):
    def test_jobs_run_one_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.admin_username = "admin"
            settings.admin_password = "admin"
            user_store = UserStore(settings)
            key_store = APIKeyStore(settings, user_store)
            manager = JobManager(settings, user_store=user_store, key_store=key_store)
            user = user_store.authenticate("admin", "admin")
            self.assertIsNotNone(user)

            lock = threading.Lock()
            running = 0
            max_running = 0

            def fake_translate(url, settings, **kwargs):
                nonlocal running, max_running
                owner_user = kwargs.get("owner_user")
                work = Work(
                    kind=WorkKind.NOVEL,
                    pixiv_id=int(url.split("=")[-1]),
                    source_url=url,
                    owner_user_id=owner_user.id if owner_user else None,
                    owner_username=owner_user.username if owner_user else None,
                    original_title="Title",
                    translated_title="Title",
                    author_name="Author",
                    chapters=[],
                )
                root = ensure_work_dirs(settings.output_dir, work)
                with lock:
                    running += 1
                    max_running = max(max_running, running)
                time.sleep(0.15)
                with lock:
                    running -= 1
                return work, root

            with patch(
                "fanfictl.jobs.translate_url_to_outputs", side_effect=fake_translate
            ):
                job1 = manager.start_job(
                    "https://www.pixiv.net/novel/show.php?id=1",
                    resume=False,
                    chapter_limit=None,
                    formats=[],
                    model=None,
                    owner_user=user,
                )
                job2 = manager.start_job(
                    "https://www.pixiv.net/novel/show.php?id=2",
                    resume=False,
                    chapter_limit=None,
                    formats=[],
                    model=None,
                    owner_user=user,
                )

                for _ in range(100):
                    state1 = manager.store.get(job1.id)
                    state2 = manager.store.get(job2.id)
                    if (
                        state1
                        and state2
                        and state1.status == state2.status == "completed"
                    ):
                        break
                    time.sleep(0.05)

            self.assertEqual(max_running, 1)
            self.assertEqual(manager.store.get(job1.id).status, "completed")
            self.assertEqual(manager.store.get(job2.id).status, "completed")
            manager.shutdown()

    def test_import_then_translate_triggers_colab_idle_stop(self) -> None:
        from fanfictl.models import Chapter, ExportFormat
        from fanfictl.storage import save_metadata

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.colab_enabled = True
            settings.colab_auto_stop = True
            user_store = UserStore(settings)
            key_store = APIKeyStore(settings, user_store)
            user = user_store.get_admin()
            self.assertIsNotNone(user)

            idle_calls: list[bool] = []

            class FakeColab:
                def schedule_idle_stop(self, fn):
                    idle_calls.append(fn())

            manager = JobManager(
                settings,
                user_store=user_store,
                key_store=key_store,
                colab_manager=FakeColab(),
            )

            work = Work(
                kind=WorkKind.NOVEL,
                pixiv_id=42,
                source_url="https://www.pixiv.net/novel/show.php?id=42",
                public_id="pub42",
                owner_user_id=user.id,
                owner_username=user.username,
                original_title="原題",
                author_name="Author",
                chapters=[
                    Chapter(
                        position=1,
                        pixiv_novel_id=42,
                        original_title="原題",
                        source_markdown="# 原題\n\n本文\n",
                    )
                ],
            )

            def fake_import(url, cfg, **kwargs):
                root = ensure_work_dirs(cfg.output_dir, work)
                save_metadata(root, work)
                return work, root

            def fake_translate_stored(work_root, cfg, **kwargs):
                work.translated_title = "Translated"
                work.target_language = "Spanish"
                work.chapters[0].translated_markdown = "# Translated\n\nTexto\n"
                save_metadata(work_root, work)
                return work, work_root

            with (
                patch("fanfictl.jobs.import_url_to_library", side_effect=fake_import),
                patch(
                    "fanfictl.jobs.translate_stored_work",
                    side_effect=fake_translate_stored,
                ),
            ):
                imp = manager.start_import_job(
                    work.source_url, chapter_limit=None, owner_user=user
                )
                for _ in range(100):
                    if manager.store.get(imp.id).status == "completed":
                        break
                    time.sleep(0.02)
                imp_done = manager.store.get(imp.id)
                self.assertEqual(imp_done.status, "completed")
                self.assertEqual(imp_done.job_type, "import")
                self.assertEqual(idle_calls, [])

                tr = manager.start_translate_job(
                    imp_done.work_root_name,
                    resume=True,
                    formats=[ExportFormat.MD],
                    target_language="Spanish",
                    owner_user=user,
                )
                for _ in range(100):
                    if manager.store.get(tr.id).status == "completed":
                        break
                    time.sleep(0.02)
                self.assertEqual(manager.store.get(tr.id).status, "completed")

            self.assertEqual(idle_calls, [False])
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
