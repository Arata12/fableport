from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from fanfictl.auth import UserRecord, UserStore
from fanfictl.config import Settings
from fanfictl.keystore import APIKeyStore
from fanfictl.library import ensure_public_id
from fanfictl.models import ExportFormat
from fanfictl.pixiv import parse_pixiv_url
from fanfictl.storage import atomic_write_text, save_metadata
from fanfictl.workflow import translate_url_to_outputs


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JobRecord(BaseModel):
    id: str
    source_url: str
    work_kind: str | None = None
    pixiv_id: int | None = None
    owner_user_id: int | None = None
    owner_username: str | None = None
    resume: bool = False
    chapter_limit: int | None = None
    formats: list[str] = Field(default_factory=list)
    model: str | None = None
    status: str = "queued"
    message: str = "Waiting for global translation slot"
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
        work_kind: str | None = None,
        pixiv_id: int | None = None,
        owner_user: UserRecord | None = None,
        resume: bool = False,
        chapter_limit: int | None = None,
        formats: list[ExportFormat] | None = None,
        model: str | None = None,
    ) -> JobRecord:
        job = JobRecord(
            id=uuid.uuid4().hex[:12],
            source_url=source_url,
            work_kind=work_kind,
            pixiv_id=pixiv_id,
            owner_user_id=owner_user.id if owner_user else None,
            owner_username=owner_user.username if owner_user else None,
            resume=resume,
            chapter_limit=chapter_limit,
            formats=[fmt.value for fmt in (formats or [])],
            model=model,
        )
        return self.save(job)

    def get(self, job_id: str) -> JobRecord | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        return JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list_all(self) -> list[JobRecord]:
        records: list[JobRecord] = []
        for path in self.base_dir.glob("*.json"):
            records.append(
                JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
            )
        return records

    def list_recent(self, limit: int = 10) -> list[JobRecord]:
        return sorted(self.list_all(), key=lambda job: job.updated_at, reverse=True)[
            :limit
        ]

    def next_queued(self) -> JobRecord | None:
        queued = [job for job in self.list_all() if job.status == "queued"]
        queued.sort(key=lambda job: (job.created_at, job.id))
        return queued[0] if queued else None


class JobManager:
    def __init__(
        self,
        settings: Settings,
        *,
        user_store: UserStore,
        key_store: APIKeyStore,
    ) -> None:
        self.settings = settings
        self.user_store = user_store
        self.key_store = key_store
        self.store = JobStore(settings.output_dir)
        self._condition = threading.Condition()
        self.reconcile_stale_jobs()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def reconcile_stale_jobs(self) -> None:
        for job in self.store.list_all():
            if job.status == "running":
                job.status = "failed"
                job.current_step = "interrupted"
                job.finished_at = utc_now()
                job.error_message = "The app restarted before this job finished. Submit it again with resume enabled."
                job.message = job.error_message
                self.store.save(job)

    def start_job(
        self,
        source_url: str,
        *,
        resume: bool,
        chapter_limit: int | None,
        formats: list[ExportFormat],
        model: str | None,
        owner_user: UserRecord | None,
    ) -> JobRecord:
        parsed = parse_pixiv_url(source_url)
        existing = self._find_active_job(parsed.kind, parsed.pixiv_id, owner_user)
        if existing:
            return existing

        job = self.store.create(
            parsed.url,
            work_kind=parsed.kind,
            pixiv_id=parsed.pixiv_id,
            owner_user=owner_user,
            resume=resume,
            chapter_limit=chapter_limit,
            formats=formats,
            model=model,
        )
        with self._condition:
            self._condition.notify_all()
        return job

    def _find_active_job(
        self, work_kind: str, pixiv_id: int, owner_user: UserRecord | None
    ) -> JobRecord | None:
        owner_id = owner_user.id if owner_user else None
        for job in self.store.list_all():
            if (
                job.work_kind == work_kind
                and job.pixiv_id == pixiv_id
                and job.status in {"queued", "running"}
                and job.owner_user_id == owner_id
            ):
                return job
        return None

    def _worker_loop(self) -> None:
        while True:
            job = self.store.next_queued()
            if not job:
                with self._condition:
                    self._condition.wait(timeout=1.0)
                continue
            self._run_job(job.id)
            time.sleep(0.05)

    def _run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job:
            return

        job.status = "running"
        job.current_step = "starting"
        job.message = "Starting translation"
        job.started_at = utc_now()
        self.store.save(job)

        owner_user = (
            self.user_store.get_user(job.owner_user_id) if job.owner_user_id else None
        )

        try:
            work, root = translate_url_to_outputs(
                job.source_url,
                self.settings,
                formats=[ExportFormat(fmt) for fmt in job.formats]
                or [
                    ExportFormat.MD,
                    ExportFormat.TXT,
                    ExportFormat.HTML,
                    ExportFormat.EPUB,
                ],
                resume=job.resume,
                chapter_limit=job.chapter_limit,
                model=job.model,
                owner_user=owner_user,
                key_store=self.key_store,
                progress_callback=lambda step,
                current,
                total,
                detail: self._update_progress(
                    job_id,
                    step,
                    current,
                    total,
                    detail,
                ),
            )
            work = ensure_public_id(root, work)
            save_metadata(root, work)
            job = self.store.get(job_id) or job
            job.status = "completed"
            job.current_step = "completed"
            job.message = "Done"
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
