from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from fanfictl.config import Settings
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
    status: str = "queued"
    message: str = "Queued"
    current_step: str = "queued"
    current_chapter: int = 0
    total_chapters: int = 0
    work_root_name: str | None = None
    public_id: str | None = None
    work_title: str | None = None
    error_message: str | None = None
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
    ) -> JobRecord:
        job = JobRecord(
            id=uuid.uuid4().hex[:12],
            source_url=source_url,
            work_kind=work_kind,
            pixiv_id=pixiv_id,
        )
        return self.save(job)

    def get(self, job_id: str) -> JobRecord | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        return JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list_recent(self, limit: int = 10) -> list[JobRecord]:
        records: list[JobRecord] = []
        for path in sorted(
            self.base_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            records.append(
                JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
            )
        return records[:limit]


class JobManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = JobStore(settings.output_dir)
        self.reconcile_stale_jobs()

    def reconcile_stale_jobs(self) -> None:
        for job in self.store.list_recent(limit=1000):
            if job.status == "running":
                job.status = "failed"
                job.current_step = "interrupted"
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
    ) -> JobRecord:
        parsed = parse_pixiv_url(source_url)
        existing = self._find_active_job(parsed.kind, parsed.pixiv_id)
        if existing:
            return existing

        job = self.store.create(
            parsed.url,
            work_kind=parsed.kind,
            pixiv_id=parsed.pixiv_id,
        )
        thread = threading.Thread(
            target=self._run_job,
            args=(job.id, parsed.url, resume, chapter_limit, formats, model),
            daemon=True,
        )
        thread.start()
        return job

    def _find_active_job(self, work_kind: str, pixiv_id: int) -> JobRecord | None:
        for job in self.store.list_recent(limit=1000):
            if (
                job.work_kind == work_kind
                and job.pixiv_id == pixiv_id
                and job.status in {"queued", "running"}
            ):
                return job
        return None

    def _run_job(
        self,
        job_id: str,
        source_url: str,
        resume: bool,
        chapter_limit: int | None,
        formats: list[ExportFormat],
        model: str | None,
    ) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        job.status = "running"
        job.current_step = "starting"
        job.message = "Starting translation"
        self.store.save(job)

        try:
            work, root = translate_url_to_outputs(
                source_url,
                self.settings,
                formats=formats,
                resume=resume,
                chapter_limit=chapter_limit,
                model=model,
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
            self.store.save(job)
        except Exception as exc:  # noqa: BLE001
            job = self.store.get(job_id) or job
            job.status = "failed"
            job.current_step = "failed"
            job.error_message = str(exc)
            job.message = str(exc)
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
