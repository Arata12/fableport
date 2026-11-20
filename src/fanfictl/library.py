from __future__ import annotations

import json
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

import bleach

from fanfictl.exporters import MARKDOWN, build_combined_markdown, output_filename
from fanfictl.models import ExportFormat, Work
from fanfictl.storage import save_metadata, slugify


ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + [
    "br",
    "img",
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
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "rel", "target"],
    "img": ["src", "alt", "title"],
}


@dataclass
class WorkEntry:
    work: Work
    root: Path
    root_name: str
    updated_at: float
    public_url_path: str
    outputs: dict[str, str]


def new_public_id() -> str:
    return secrets.token_hex(6)


def ensure_public_id(root: Path, work: Work) -> Work:
    """Give legacy works (created before public ids existed) a public id."""
    if not work.public_id:
        work.public_id = new_public_id()
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


def get_outputs(root: Path, work: Work) -> dict[str, str]:
    names = {fmt.value: output_filename(work, fmt) for fmt in ExportFormat}
    return {fmt: name for fmt, name in names.items() if (root / name).exists()}


def work_public_url_path(work: Work) -> str:
    title = work.translated_title or work.original_title
    return f"/read/{work.public_id}-{slugify(title)}"


def _entry(root: Path) -> WorkEntry:
    work = load_work(root)
    return WorkEntry(
        work=work,
        root=root,
        root_name=root.name,
        updated_at=root.stat().st_mtime,
        public_url_path=work_public_url_path(work),
        outputs=get_outputs(root, work),
    )


def list_works(base_dir: Path) -> list[WorkEntry]:
    return [_entry(root) for root in iter_work_roots(base_dir)]


def get_work_by_root_name(base_dir: Path, root_name: str) -> WorkEntry | None:
    # Only visible direct children of the output dir (rejects "..", ".jobs", "a/b").
    if root_name.startswith(".") or "/" in root_name or "\\" in root_name:
        return None
    root = base_dir / root_name
    if not (root / "metadata.json").exists():
        return None
    return _entry(root)


def delete_work_by_root_name(base_dir: Path, root_name: str) -> WorkEntry | None:
    entry = get_work_by_root_name(base_dir, root_name)
    if not entry:
        return None
    resolved_base = base_dir.resolve()
    resolved_root = entry.root.resolve()
    if resolved_root.parent != resolved_base:
        return None
    if entry.work.public_id:
        _public_id_roots.pop(entry.work.public_id, None)
    shutil.rmtree(resolved_root)
    return entry


# public_id -> work folder. Public ids never change, so this avoids parsing
# every work's metadata (which holds the full text) on each reader/asset request.
_public_id_roots: dict[str, Path] = {}


def get_work_by_public_id(base_dir: Path, public_id: str) -> WorkEntry | None:
    root = _public_id_roots.get(public_id)
    if root and root.parent == base_dir and (root / "metadata.json").exists():
        entry = _entry(root)
        if entry.work.public_id == public_id:
            return entry
    for root in iter_work_roots(base_dir):
        work_id = load_work(root).public_id
        if work_id:
            _public_id_roots[work_id] = root
        if work_id == public_id:
            return _entry(root)
    return None


def render_work_html(work: Work) -> str:
    # The reader header already shows the title and summary, so render only
    # the text (a single novel's chapter heading is the work title too).
    if len(work.chapters) == 1:
        return render_chapter_html(work, 1)
    return sanitize_html(MARKDOWN.render(build_combined_markdown(work)))


def render_chapter_html(work: Work, chapter_no: int) -> str:
    chapter = work.chapters[chapter_no - 1]
    markdown = chapter.translated_markdown or chapter.source_markdown
    # The chapter page header already shows the chapter title.
    markdown = re.sub(r"\A\s*#\s[^\n]*\n*", "", markdown, count=1)
    return sanitize_html(MARKDOWN.render(markdown))


def sanitize_html(value: str) -> str:
    return bleach.clean(
        value, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, strip=True
    )
