from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from fanfictl.auth import UserRecord, UserStore
from fanfictl.colab import ColabManager
from fanfictl.config import Settings
from fanfictl.keystore import APIKeyStore
from fanfictl.library import get_work_by_root_name
from fanfictl.models import ExportFormat
from fanfictl.pixiv import parse_pixiv_url
from fanfictl.pixiv_tokens import PixivTokenStore
from fanfictl.storage import atomic_write_text
from fanfictl.workflow import (
    import_url_to_library,
    translate_stored_work,
    translate_url_to_outputs,
)


def utc_now() -> str:
    # Full precision: the queue is ordered by created_at (FIFO).
    return datetime.now(UTC).isoformat()


JobType = Literal["import", "translate", "import_and_translate"]


class JobRecord(BaseModel):
    id: str
    job_type: JobType = "import_and_translate"
    source_url: str
    work_kind: str | None = None
    pixiv_id: int | None = None
    owner_user_id: int | None = None
    owner_username: str | None = None
    resume: bool = False
    refresh_source: bool = False
    chapter_limit: int | None = None
    formats: list[str] = Field(default_factory=list)
    model: str | None = None
    target_language: str | None = None
    status: str = "queued"
    message: str = "Waiting in queue"
    current_step: str = "queued"
    current_chapter: int = 0
    total_chapters: int = 0
    work_root_name: str | None = None
    public_id: str | None = None
    work_title: str | None = None
    error_message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class JobStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir / ".jobs"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, job_id: str) -> Path:
        return self.base_dir / f"{job_id}.json"

    def save(self, job: JobRecord) -> JobRecord:
        job.updated_at = utc_now()
        with self._lock:
            atomic_write_text(self._path(job.id), job.model_dump_json(indent=2))
        return job

    def create(
        self,
        source_url: str,
        *,
        job_type: JobType = "import_and_translate",
        work_kind: str | None = None,
        pixiv_id: int | None = None,
        owner_user: UserRecord | None = None,
        resume: bool = False,
        refresh_source: bool = False,
        chapter_limit: int | None = None,
        formats: list[ExportFormat] | None = None,
        model: str | None = None,
        target_language: str | None = None,
        work_root_name: str | None = None,
        public_id: str | None = None,
        work_title: str | None = None,
        total_chapters: int = 0,
    ) -> JobRecord:
        default_msg = (
            "Waiting for import slot"
            if job_type == "import"
            else "Waiting for global translation slot"
        )
        job = JobRecord(
            id=uuid.uuid4().hex[:12],
            job_type=job_type,
            source_url=source_url,
            work_kind=work_kind,
            pixiv_id=pixiv_id,
            owner_user_id=owner_user.id if owner_user else None,
            owner_username=owner_user.username if owner_user else None,
            resume=resume,
            refresh_source=refresh_source,
            chapter_limit=chapter_limit,
            formats=[fmt.value for fmt in (formats or [])],
            model=model,
            target_language=target_language,
            message=default_msg,
            work_root_name=work_root_name,
            public_id=public_id,
            work_title=work_title,
            total_chapters=total_chapters,
        )
        return self.save(job)

    def get(self, job_id: str) -> JobRecord | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        return JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list_all(self) -> list[JobRecord]:
        return [
            JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
            for path in self.base_dir.glob("*.json")
        ]

    def list_recent(self, limit: int = 12) -> list[JobRecord]:
        return sorted(self.list_all(), key=lambda job: job.updated_at, reverse=True)[
            :limit
        ]

    def next_queued(
        self, *, job_types: set[str] | None = None
    ) -> JobRecord | None:
        queued = [
            job
            for job in self.list_all()
            if job.status == "queued"
            and (job_types is None or job.job_type in job_types)
        ]
        return min(queued, key=lambda job: (job.created_at, job.id), default=None)

    def delete(self, job_id: str) -> None:
        with self._lock:
            path = self._path(job_id)
            if path.exists():
                path.unlink()


_IMPORT_TYPES = {"import"}
_TRANSLATE_TYPES = {"translate", "import_and_translate"}


class JobManager:
    def __init__(
        self,
        settings: Settings,
        *,
        user_store: UserStore,
        key_store: APIKeyStore,
        pixiv_token_store: PixivTokenStore | None = None,
        colab_manager: ColabManager | None = None,
    ) -> None:
        self.settings = settings
        self.user_store = user_store
        self.key_store = key_store
        self.pixiv_token_store = pixiv_token_store or PixivTokenStore(
            settings, user_store
        )
        self.colab_manager = colab_manager
        self.store = JobStore(settings.output_dir)
        self._import_cond = threading.Condition()
        self._translate_cond = threading.Condition()
        self._stop_event = threading.Event()
        self.reconcile_stale_jobs()
        self._import_worker = threading.Thread(
            target=self._worker_loop,
            args=(_IMPORT_TYPES, self._import_cond),
            daemon=True,
            name="fableport-import-worker",
        )
        self._translate_worker = threading.Thread(
            target=self._worker_loop,
            args=(_TRANSLATE_TYPES, self._translate_cond),
            daemon=True,
            name="fableport-translate-worker",
        )
        self._import_worker.start()
        self._translate_worker.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._import_cond:
            self._import_cond.notify_all()
        with self._translate_cond:
            self._translate_cond.notify_all()

    def reconcile_stale_jobs(self) -> None:
        for job in self.store.list_all():
            if job.status == "running":
                job.status = "failed"
                job.current_step = "interrupted"
                job.finished_at = utc_now()
                job.error_message = (
                    "The app restarted before this job finished. "
                    "You can retry or resume it from Library."
                )
                job.message = job.error_message
                self.store.save(job)

    def has_active_translation_jobs(self) -> bool:
        return any(
            job.status in {"queued", "running"} and job.job_type in _TRANSLATE_TYPES
            for job in self.store.list_all()
        )

    def has_active_job_for_work(self, work_root_name: str) -> bool:
        return any(
            job.work_root_name == work_root_name
            and job.status in {"queued", "running"}
            for job in self.store.list_all()
        )

    def delete_finished_jobs_for_work(self, work_root_name: str) -> None:
        for job in self.store.list_all():
            if (
                job.work_root_name == work_root_name
                and job.status not in {"queued", "running"}
            ):
                self.store.delete(job.id)

    def start_import_job(
        self,
        source_url: str,
        *,
        chapter_limit: int | None,
        owner_user: UserRecord | None,
    ) -> JobRecord:
        parsed = parse_pixiv_url(source_url)
        existing = self._find_active_job(parsed.kind, parsed.pixiv_id, owner_user)
        if existing:
            return existing

        job = self.store.create(
            parsed.url,
            job_type="import",
            work_kind=parsed.kind,
            pixiv_id=parsed.pixiv_id,
            owner_user=owner_user,
            chapter_limit=chapter_limit,
        )
        with self._import_cond:
            self._import_cond.notify_all()
        return job

    def start_translate_job(
        self,
        work_root_name: str,
        *,
        resume: bool,
        formats: list[ExportFormat],
        model: str | None = None,
        target_language: str | None = None,
        refresh_source: bool = False,
        owner_user: UserRecord | None = None,
    ) -> JobRecord:
        entry = get_work_by_root_name(self.settings.output_dir, work_root_name)
        if not entry:
            raise ValueError(f"Work not found: {work_root_name}")
        existing = self._find_active_job(
            entry.work.kind.value,
            entry.work.pixiv_id,
            owner_user,
            work_root_name=work_root_name,
        )
        if existing:
            return existing

        job = self.store.create(
            entry.work.source_url,
            job_type="translate",
            work_kind=entry.work.kind.value,
            pixiv_id=entry.work.pixiv_id,
            owner_user=owner_user,
            resume=resume,
            refresh_source=refresh_source,
            formats=formats,
            model=model,
            target_language=target_language or self.settings.target_language,
            work_root_name=entry.root_name,
            public_id=entry.work.public_id,
            work_title=entry.work.translated_title or entry.work.original_title,
            total_chapters=len(entry.work.chapters),
        )
        with self._translate_cond:
            self._translate_cond.notify_all()
        return job

    def start_job(
        self,
        source_url: str,
        *,
        resume: bool,
        chapter_limit: int | None,
        formats: list[ExportFormat],
        model: str | None,
        owner_user: UserRecord | None,
        target_language: str | None = None,
    ) -> JobRecord:
        parsed = parse_pixiv_url(source_url)
        existing = self._find_active_job(parsed.kind, parsed.pixiv_id, owner_user)
        if existing:
            return existing

        job = self.store.create(
            parsed.url,
            job_type="import_and_translate",
            work_kind=parsed.kind,
            pixiv_id=parsed.pixiv_id,
            owner_user=owner_user,
            resume=resume,
            chapter_limit=chapter_limit,
            formats=formats,
            model=model,
            target_language=target_language or self.settings.target_language,
        )
        with self._translate_cond:
            self._translate_cond.notify_all()
        return job

    def _find_active_job(
        self,
        work_kind: str,
        pixiv_id: int,
        owner_user: UserRecord | None,
        *,
        work_root_name: str | None = None,
    ) -> JobRecord | None:
        owner_id = owner_user.id if owner_user else None
        for job in self.store.list_all():
            if job.status not in {"queued", "running"}:
                continue
            if work_root_name and job.work_root_name == work_root_name:
                return job
            if (
                job.work_kind == work_kind
                and job.pixiv_id == pixiv_id
                and job.owner_user_id == owner_id
            ):
                return job
        return None

    def _worker_loop(
        self, job_types: set[str], condition: threading.Condition
    ) -> None:
        while not self._stop_event.is_set():
            job = self.store.next_queued(job_types=job_types)
            if not job:
                with condition:
                    condition.wait(timeout=10.0)
                continue
            self._run_job(job.id)
            if (
                job_types == _TRANSLATE_TYPES
                and self.colab_manager
                and self.settings.colab_enabled
                and not self.has_active_translation_jobs()
            ):
                self.colab_manager.schedule_idle_stop(self.has_active_translation_jobs)

    def _run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job:
            return

        job.status = "running"
        job.current_step = "starting"
        job.message = (
            "Starting import" if job.job_type == "import" else "Starting translation"
        )
        job.started_at = utc_now()
        self.store.save(job)

        owner_user = (
            self.user_store.get_user(job.owner_user_id) if job.owner_user_id else None
        )
        formats = [ExportFormat(fmt) for fmt in job.formats] or list(ExportFormat)

        try:
            if job.job_type == "import":
                work, root = import_url_to_library(
                    job.source_url,
                    self.settings,
                    chapter_limit=job.chapter_limit,
                    owner_user=owner_user,
                    pixiv_token_store=self.pixiv_token_store,
                    progress_callback=lambda *args: self._update_progress(job_id, *args),
                )
            elif job.job_type == "translate":
                if not job.work_root_name:
                    raise RuntimeError("Translation job is missing work_root_name")
                work_root = self.settings.output_dir / job.work_root_name
                work, root = translate_stored_work(
                    work_root,
                    self.settings,
                    formats=formats,
                    resume=job.resume,
                    model=job.model,
                    target_language=job.target_language,
                    refresh_source=job.refresh_source,
                    chapter_limit=job.chapter_limit,
                    owner_user=owner_user,
                    key_store=self.key_store,
                    pixiv_token_store=self.pixiv_token_store,
                    colab_manager=self.colab_manager,
                    progress_callback=lambda *args: self._update_progress(job_id, *args),
                )
            else:
                work, root = translate_url_to_outputs(
                    job.source_url,
                    self.settings,
                    formats=formats,
                    resume=job.resume,
                    chapter_limit=job.chapter_limit,
                    model=job.model,
                    target_language=job.target_language,
                    owner_user=owner_user,
                    key_store=self.key_store,
                    pixiv_token_store=self.pixiv_token_store,
                    colab_manager=self.colab_manager,
                    progress_callback=lambda *args: self._update_progress(job_id, *args),
                )

            job = self.store.get(job_id) or job
            job.status = "completed"
            job.current_step = "completed"
            job.message = (
                f"Imported ({len(work.chapters)} chapter(s), ready to translate)"
                if job.job_type == "import"
                else "Done"
            )
            job.current_chapter = len(work.chapters)
            job.total_chapters = len(work.chapters)
            job.work_root_name = root.name
            job.public_id = work.public_id
            job.work_title = work.translated_title or work.original_title
            job.finished_at = utc_now()
            self.store.save(job)
        except Exception as exc:  # noqa: BLE001
            job = self.store.get(job_id) or job
            job.status = "failed"
            job.current_step = "failed"
            job.error_message = str(exc)
            job.message = str(exc)
            job.finished_at = utc_now()
            self.store.save(job)

    def _update_progress(
        self,
        job_id: str,
        step: str,
        current: int,
        total: int,
        detail: str,
    ) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        job.status = "running"
        job.current_step = step
        job.current_chapter = current
        job.total_chapters = total
        job.message = detail
        self.store.save(job)
