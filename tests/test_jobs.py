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


if __name__ == "__main__":
    unittest.main()
