from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable

from fanfictl.auth import UserRecord
from fanfictl.config import Settings
from fanfictl.exporters import (
    build_combined_markdown,
    write_epub,
    write_html,
    write_markdown,
    write_text,
)
from fanfictl.keystore import APIKeyStore
from fanfictl.models import Checkpoint, ExportFormat, Work, WorkKind
from fanfictl.pixiv import (
    AuthenticatedPixivClient,
    PixivAccessError,
    PixivClient,
    parse_pixiv_url,
)
from fanfictl.pixiv_tokens import PixivTokenStore
from fanfictl.quota import QuotaTracker
from fanfictl.storage import (
    ensure_work_dirs,
    load_checkpoint,
    save_checkpoint,
    save_metadata,
)
from fanfictl.translate import GeminiStudioProvider, translate_work


ProgressCallback = Callable[[str, int, int, str], None]


def fetch_work_from_url(
    url: str,
    *,
    chapter_limit: int | None = None,
    owner_user: UserRecord | None = None,
    pixiv_token_store: PixivTokenStore | None = None,
) -> Work:
    parsed = parse_pixiv_url(url)
    client = PixivClient()
    try:
        work = (
            client.fetch_novel_work(parsed.pixiv_id, parsed.url)
            if parsed.kind == "novel"
            else client.fetch_series_work(parsed.pixiv_id, parsed.url)
        )
    except PixivAccessError as public_error:
        token_store = pixiv_token_store
        tokens = token_store.runtime_tokens_for_user(owner_user) if token_store else []
        if not tokens:
            raise RuntimeError(
                "This Pixiv work requires login to import. Add a Pixiv refresh token in Settings."
            ) from public_error
        auth_client = AuthenticatedPixivClient(tokens)
        work = (
            auth_client.fetch_novel_work(parsed.pixiv_id, parsed.url)
            if parsed.kind == "novel"
            else auth_client.fetch_series_work(parsed.pixiv_id, parsed.url)
        )
    finally:
        client.close()

    if chapter_limit:
        work.chapters = work.chapters[:chapter_limit]
    return work


def translate_url_to_outputs(
    url: str,
    settings: Settings,
    *,
    target: str = "en",
    output: Path | None = None,
    formats: Iterable[ExportFormat] = (
        ExportFormat.MD,
        ExportFormat.TXT,
        ExportFormat.HTML,
        ExportFormat.EPUB,
    ),
    resume: bool = False,
    chapter_limit: int | None = None,
    model: str | None = None,
    progress_callback: ProgressCallback | None = None,
    owner_user: UserRecord | None = None,
    key_store: APIKeyStore | None = None,
) -> tuple[Work, Path]:
    if target.lower() != "en":
        raise ValueError("v1 only supports English output")
    key_store = key_store or APIKeyStore(settings)
    pixiv_token_store = PixivTokenStore(settings)
    runtime_keys = key_store.runtime_keys_for_user(owner_user)
    if not runtime_keys:
        raise RuntimeError(
            "At least one Gemini API key is required. Put one in .env or add fallback keys in the web dashboard."
        )

    if progress_callback:
        progress_callback("fetching", 0, 0, "Fetching Pixiv work")

    work = fetch_work_from_url(
        url,
        chapter_limit=chapter_limit,
        owner_user=owner_user,
        pixiv_token_store=pixiv_token_store,
    )
    work.owner_user_id = owner_user.id if owner_user else None
    work.owner_username = owner_user.username if owner_user else None
    output_base = (output or settings.output_dir).resolve()
    work_root = ensure_work_dirs(output_base, work)
    existing_work = _load_existing_work(work_root)
    if existing_work:
        work.public_id = existing_work.public_id
        work.owner_user_id = existing_work.owner_user_id
        work.owner_username = existing_work.owner_username
        if existing_work.translated_title and not resume:
            work.translated_title = existing_work.translated_title
        if existing_work.translated_description and not resume:
            work.translated_description = existing_work.translated_description
    checkpoint = load_checkpoint(work_root) if resume else None
    if checkpoint is None:
        checkpoint = Checkpoint(
            source_url=work.source_url,
            kind=work.kind,
            pixiv_id=work.pixiv_id,
            original_title=work.original_title,
            model_name=model or settings.gemini_model,
        )

    provider = GeminiStudioProvider(
        api_keys=runtime_keys,
        model_name=model or settings.gemini_model,
        quota_tracker=QuotaTracker(settings, runtime_keys),
    )

    if progress_callback:
        progress_callback("translating", 0, len(work.chapters), "Starting translation")

    work = translate_work(
        work,
        provider,
        checkpoint,
        checkpoint_callback=lambda cp: save_checkpoint(work_root, cp),
        progress_callback=progress_callback,
    )

    save_checkpoint(work_root, checkpoint)
    save_metadata(work_root, work)

    for chapter in work.chapters:
        chapter_root = work_root / "chapters"
        (chapter_root / f"{chapter.position:03d}-source.md").write_text(
            chapter.source_markdown,
            encoding="utf-8",
        )
        if chapter.translated_markdown:
            (chapter_root / f"{chapter.position:03d}-translated.md").write_text(
                chapter.translated_markdown,
                encoding="utf-8",
            )

    if progress_callback:
        progress_callback(
            "exporting", len(work.chapters), len(work.chapters), "Rendering exports"
        )

    combined_markdown = build_combined_markdown(work)
    title = work.translated_title or work.original_title
    formats = set(formats)
    if ExportFormat.MD in formats:
        write_markdown(
            work_root
            / ("translated.md" if work.kind == WorkKind.NOVEL else "combined.md"),
            combined_markdown,
        )
    if ExportFormat.TXT in formats:
        write_text(
            work_root
            / ("translated.txt" if work.kind == WorkKind.NOVEL else "combined.txt"),
            combined_markdown,
        )
    if ExportFormat.HTML in formats:
        write_html(
            work_root
            / ("translated.html" if work.kind == WorkKind.NOVEL else "combined.html"),
            combined_markdown,
            title,
        )
    if ExportFormat.EPUB in formats:
        write_epub(
            work_root
            / ("translated.epub" if work.kind == WorkKind.NOVEL else "combined.epub"),
            work,
        )

    if progress_callback:
        progress_callback("completed", len(work.chapters), len(work.chapters), "Done")

    save_metadata(work_root, work)
    return work, work_root


def _load_existing_work(work_root: Path) -> Work | None:
    metadata_path = work_root / "metadata.json"
    if not metadata_path.exists():
        return None
    return Work.model_validate(json.loads(metadata_path.read_text(encoding="utf-8")))
