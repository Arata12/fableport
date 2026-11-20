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
    def test_jobs_run_one_at_a_time_when_concurrency_is_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.admin_username = "admin"
            settings.admin_password = "admin"
            settings.translation_concurrency = 1
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

    def test_independent_works_run_in_parallel_up_to_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.translation_concurrency = 3
            user_store = UserStore(settings)
            key_store = APIKeyStore(settings, user_store)
            manager = JobManager(settings, user_store=user_store, key_store=key_store)
            user = user_store.get_admin()
            self.assertIsNotNone(user)

            lock = threading.Lock()
            running = 0
            max_running = 0
            three_active = threading.Event()
            seen_pools = []

            def fake_translate(url, cfg, **kwargs):
                nonlocal running, max_running
                owner_user = kwargs.get("owner_user")
                seen_pools.append(kwargs.get("slot_pool"))
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
                root = ensure_work_dirs(cfg.output_dir, work)
                with lock:
                    running += 1
                    max_running = max(max_running, running)
                    if running == 3:
                        three_active.set()
                three_active.wait(timeout=2.0)
                time.sleep(0.08)
                with lock:
                    running -= 1
                return work, root

            with patch(
                "fanfictl.jobs.translate_url_to_outputs", side_effect=fake_translate
            ):
                jobs = [
                    manager.start_job(
                        f"https://www.pixiv.net/novel/show.php?id={idx}",
                        resume=False,
                        chapter_limit=None,
                        formats=[],
                        model=None,
                        owner_user=user,
                    )
                    for idx in range(1, 5)
                ]
                for _ in range(200):
                    if all(
                        (manager.store.get(j.id).status == "completed")
                        for j in jobs
                    ):
                        break
                    time.sleep(0.02)

            self.assertEqual(max_running, 3)
            self.assertTrue(all(p is not None for p in seen_pools))
            self.assertEqual(len({id(p) for p in seen_pools}), 1)
            for j in jobs:
                self.assertEqual(manager.store.get(j.id).status, "completed")
            manager.shutdown()

    def test_shared_slot_pool_caps_series_and_oneshots_together(self) -> None:
        from fanfictl.models import Chapter, Checkpoint
        from fanfictl.translate import TranslationSlotPool, translate_work

        pool = TranslationSlotPool(lambda: 3)
        lock = threading.Lock()
        active = 0
        max_active = 0

        class SlowProvider:
            context_length = 32768

            def translate_chunk(self, text: str, previous_context: str | None = None) -> str:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.08)
                with lock:
                    active -= 1
                return text

            def translate_title(self, title: str) -> str:
                return title

            def translate_description(self, desc: str) -> str:
                return desc

        def make_work(pixiv_id: int, num_chapters: int) -> Work:
            return Work(
                kind=WorkKind.SERIES if num_chapters > 1 else WorkKind.NOVEL,
                pixiv_id=pixiv_id,
                source_url=f"https://www.pixiv.net/novel/show.php?id={pixiv_id}",
                original_title=f"Work {pixiv_id}",
                author_name="Author",
                chapters=[
                    Chapter(
                        position=i,
                        pixiv_novel_id=pixiv_id * 100 + i,
                        original_title=f"Ch {i}",
                        source_markdown=f"# Ch {i}\n\nBody {i}\n",
                    )
                    for i in range(1, num_chapters + 1)
                ],
            )

        works = [make_work(1, 4), make_work(2, 1), make_work(3, 1)]
        threads = [
            threading.Thread(
                target=translate_work,
                args=(
                    w,
                    SlowProvider(),
                    Checkpoint(
                        source_url=w.source_url,
                        kind=w.kind,
                        pixiv_id=w.pixiv_id,
                        original_title=w.original_title,
                        model_name="test-model",
                    ),
                ),
                kwargs={"max_workers": 3, "slot_pool": pool},
            )
            for w in works
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(max_active, 3)
        for w in works:
            self.assertTrue(all(ch.translated_markdown for ch in w.chapters))

    def test_cancel_queued_translations_marks_waiting_jobs_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.translation_concurrency = 1
            user_store = UserStore(settings)
            key_store = APIKeyStore(settings, user_store)
            manager = JobManager(settings, user_store=user_store, key_store=key_store)
            user = user_store.get_admin()
            started = threading.Event()
            release = threading.Event()

            def blocking_translate(url, cfg, **kwargs):
                started.set()
                release.wait(timeout=5.0)
                work = Work(
                    kind=WorkKind.NOVEL,
                    pixiv_id=int(url.split("=")[-1]),
                    source_url=url,
                    original_title="Title",
                    translated_title="Title",
                    author_name="Author",
                    chapters=[],
                )
                return work, ensure_work_dirs(cfg.output_dir, work)

            with patch(
                "fanfictl.jobs.translate_url_to_outputs", side_effect=blocking_translate
            ):
                j1 = manager.start_job(
                    "https://www.pixiv.net/novel/show.php?id=10",
                    resume=False,
                    chapter_limit=None,
                    formats=[],
                    model=None,
                    owner_user=user,
                )
                self.assertTrue(started.wait(timeout=3.0))
                j2 = manager.start_job(
                    "https://www.pixiv.net/novel/show.php?id=11",
                    resume=False,
                    chapter_limit=None,
                    formats=[],
                    model=None,
                    owner_user=user,
                )
                cancelled = manager.cancel_queued_translations("Stopped by test")
                self.assertEqual(cancelled, 1)
                self.assertEqual(manager.store.get(j2.id).status, "failed")
                self.assertEqual(manager.store.get(j2.id).message, "Stopped by test")
                release.set()
                for _ in range(100):
                    if manager.store.get(j1.id).status == "completed":
                        break
                    time.sleep(0.02)
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
