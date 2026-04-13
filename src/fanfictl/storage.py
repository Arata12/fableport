from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from fanfictl.models import Checkpoint, Work, WorkKind


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9\s-]", "", value)
    value = re.sub(r"[-\s]+", "-", value)
    return value.strip("-") or "untitled"


def work_output_dir(base_dir: Path, work: Work) -> Path:
    prefix = "novel" if work.kind == WorkKind.NOVEL else "series"
    if work.owner_user_id is not None:
        return base_dir / f"{prefix}-{work.pixiv_id}-u{work.owner_user_id}"
    return base_dir / f"{prefix}-{work.pixiv_id}"


def ensure_work_dirs(base_dir: Path, work: Work) -> Path:
    root = work_output_dir(base_dir, work)
    (root / "chapters").mkdir(parents=True, exist_ok=True)
    return root


def save_metadata(root: Path, work: Work) -> None:
    atomic_write_text(root / "metadata.json", work.model_dump_json(indent=2))


def save_checkpoint(root: Path, checkpoint: Checkpoint) -> None:
    atomic_write_text(root / "checkpoint.json", checkpoint.model_dump_json(indent=2))


def load_checkpoint(root: Path) -> Checkpoint | None:
    path = root / "checkpoint.json"
    if not path.exists():
        return None
    return Checkpoint.model_validate(json.loads(path.read_text(encoding="utf-8")))


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=path.parent
    ) as handle:
        handle.write(content)
        temp_name = handle.name
    Path(temp_name).replace(path)
