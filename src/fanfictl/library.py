from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

import bleach
from markdown_it import MarkdownIt

from fanfictl.exporters import build_combined_markdown
from fanfictl.models import ExportFormat, Work, WorkKind
from fanfictl.storage import save_metadata, slugify


MARKDOWN = MarkdownIt("commonmark", {"html": True, "linkify": True})
ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + [
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "pre",
    "ruby",
    "rt",
]
ALLOWED_ATTRIBUTES = {"a": ["href", "title", "rel", "target"]}


@dataclass
class WorkEntry:
    work: Work
    root: Path
    root_name: str
    updated_at: float
    public_url_path: str
    outputs: dict[str, str]


def ensure_public_id(root: Path, work: Work) -> Work:
    if not work.public_id:
        work.public_id = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
        save_metadata(root, work)
    return work


def iter_work_roots(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    return sorted(
        [
            child
            for child in base_dir.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and (child / "metadata.json").exists()
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def load_work(root: Path) -> Work:
    payload = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    work = Work.model_validate(payload)
    return ensure_public_id(root, work)


def output_filename(work: Work, fmt: ExportFormat) -> str:
    stem = "translated" if work.kind == WorkKind.NOVEL else "combined"
    return f"{stem}.{fmt.value}"


def get_outputs(root: Path, work: Work) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for fmt in ExportFormat:
        name = output_filename(work, fmt)
        if (root / name).exists():
            outputs[fmt.value] = name
    return outputs


def work_public_url_path(work: Work) -> str:
    title = work.translated_title or work.original_title
    return f"/read/{work.public_id}-{slugify(title)}"


def list_works(base_dir: Path) -> list[WorkEntry]:
    entries: list[WorkEntry] = []
    for root in iter_work_roots(base_dir):
        work = load_work(root)
        entries.append(
            WorkEntry(
                work=work,
                root=root,
                root_name=root.name,
                updated_at=root.stat().st_mtime,
                public_url_path=work_public_url_path(work),
                outputs=get_outputs(root, work),
            )
        )
    return entries


def get_work_by_root_name(base_dir: Path, root_name: str) -> WorkEntry | None:
    root = base_dir / root_name
    if not root.exists() or not (root / "metadata.json").exists():
        return None
    work = load_work(root)
    return WorkEntry(
        work=work,
        root=root,
        root_name=root.name,
        updated_at=root.stat().st_mtime,
        public_url_path=work_public_url_path(work),
        outputs=get_outputs(root, work),
    )


def get_work_by_public_id(base_dir: Path, public_id: str) -> WorkEntry | None:
    for entry in list_works(base_dir):
        if entry.work.public_id == public_id:
            return entry
    return None


def render_work_html(work: Work) -> str:
    return sanitize_html(MARKDOWN.render(build_combined_markdown(work)))


def render_chapter_html(work: Work, chapter_no: int) -> str:
    chapter = work.chapters[chapter_no - 1]
    return sanitize_html(
        MARKDOWN.render(chapter.translated_markdown or chapter.source_markdown)
    )


def sanitize_html(value: str) -> str:
    return bleach.clean(
        value, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, strip=True
    )
