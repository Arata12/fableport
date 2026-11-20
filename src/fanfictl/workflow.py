from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Callable, Iterable
from urllib.parse import urlparse

import httpx

from fanfictl.auth import UserRecord
from fanfictl.colab import ColabManager
from fanfictl.config import Settings
from fanfictl.content import normalize_target_language_name
from fanfictl.exporters import export_work
from fanfictl.keystore import APIKeyStore, RuntimeAPIKey
from fanfictl.library import new_public_id
from fanfictl.models import Checkpoint, ExportFormat, ParsedPixivUrl, Work
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
from fanfictl.translate import LLMProvider, TranslationSlotPool, translate_work


ProgressCallback = Callable[[str, int, int, str], None]


def parse_pixiv_urls(raw_text: str) -> list[ParsedPixivUrl]:
    """Parse one or more Pixiv novel/series URLs (or numeric IDs), one per line."""
    parsed: list[ParsedPixivUrl] = []
    seen: set[tuple[str, int]] = set()
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        item = parse_pixiv_url(line)
        key = (item.kind, item.pixiv_id)
        if key not in seen:
            seen.add(key)
            parsed.append(item)
    if not parsed:
        raise ValueError("Provide at least one Pixiv novel or series URL.")
    return parsed


def _fetch(
    client: PixivClient | AuthenticatedPixivClient, kind: str, pixiv_id: int, url: str
) -> Work:
    if kind == "novel":
        return client.fetch_novel_work(pixiv_id, url)
    return client.fetch_series_work(pixiv_id, url)


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
        work = _fetch(client, parsed.kind, parsed.pixiv_id, parsed.url)
    except PixivAccessError as public_error:
        tokens = (
            pixiv_token_store.runtime_tokens_for_user(owner_user)
            if pixiv_token_store
            else []
        )
        if not tokens:
            raise RuntimeError(
                "This Pixiv work requires login to import. Add a Pixiv refresh token in Settings."
            ) from public_error
        work = _fetch(
            AuthenticatedPixivClient(tokens), parsed.kind, parsed.pixiv_id, parsed.url
        )
    finally:
        client.close()

    if chapter_limit and chapter_limit > 0:
        work.chapters = work.chapters[:chapter_limit]
    return work


def _write_chapter_files(work: Work, work_root: Path) -> None:
    chapter_root = work_root / "chapters"
    chapter_root.mkdir(parents=True, exist_ok=True)
    for chapter in work.chapters:
        prefix = f"{chapter.position:03d}"
        (chapter_root / f"{prefix}-source.md").write_text(
            chapter.source_markdown, encoding="utf-8"
        )
        if chapter.translated_markdown:
            (chapter_root / f"{prefix}-translated.md").write_text(
                chapter.translated_markdown, encoding="utf-8"
            )


def _merge_existing_translations(work: Work, existing: Work | None) -> None:
    """When re-importing an existing work (e.g. a series with new chapters),
    preserve translations on chapters whose source text has not changed."""
    if not existing:
        return
    work.public_id = existing.public_id or work.public_id
    existing_by_id = {ch.pixiv_novel_id: ch for ch in existing.chapters}
    any_preserved = False
    for chapter in work.chapters:
        prev = existing_by_id.get(chapter.pixiv_novel_id)
        if prev and prev.is_translated and prev.source_markdown == chapter.source_markdown:
            chapter.translated_title = prev.translated_title
            chapter.translated_markdown = prev.translated_markdown
            any_preserved = True
    if any_preserved:
        work.translated_title = existing.translated_title
        work.translated_description = existing.translated_description
        work.target_language = existing.target_language


def import_url_to_library(
    url: str,
    settings: Settings,
    *,
    output: Path | None = None,
    chapter_limit: int | None = None,
    progress_callback: ProgressCallback | None = None,
    owner_user: UserRecord | None = None,
    pixiv_token_store: PixivTokenStore | None = None,
    preserve_translations: bool = True,
) -> tuple[Work, Path]:
    """Fetch a Pixiv novel or series and save its untranslated source locally."""
    pixiv_token_store = pixiv_token_store or PixivTokenStore(settings)

    def progress(stage: str, current: int, total: int, detail: str) -> None:
        if progress_callback:
            progress_callback(stage, current, total, detail)

    progress("fetching", 0, 0, "Fetching Pixiv work")
    work = fetch_work_from_url(
        url,
        chapter_limit=chapter_limit,
        owner_user=owner_user,
        pixiv_token_store=pixiv_token_store,
    )
    work.owner_user_id = owner_user.id if owner_user else None
    work.owner_username = owner_user.username if owner_user else None

    # The output dir is keyed by kind, id and owner, so re-imports land in the
    # same place and keep their public reader link.
    work_root = ensure_work_dirs((output or settings.output_dir).resolve(), work)
    existing_work = _load_existing_work(work_root)
    work.public_id = (
        existing_work.public_id
        if existing_work and existing_work.public_id
        else new_public_id()
    )

    progress("assets", 0, len(work.chapters), "Saving source chapters and images")
    _localize_pixiv_assets(work, work_root)
    if preserve_translations:
        _merge_existing_translations(work, existing_work)
    _write_chapter_files(work, work_root)
    save_metadata(work_root, work)

    total = len(work.chapters)
    progress("completed", total, total, f"Imported {total} chapter(s)")
    return work, work_root


def _resolve_provider(
    settings: Settings,
    *,
    model: str | None,
    target_language: str,
    owner_user: UserRecord | None,
    key_store: APIKeyStore,
    colab_manager: ColabManager | None,
    progress_callback: ProgressCallback | None,
) -> tuple[LLMProvider, str]:
    if settings.colab_enabled or settings.provider_backend == "colab":
        if not colab_manager:
            raise RuntimeError(
                "Managed Google Colab is enabled in Settings, but no Colab manager is active."
            )
        if not settings.colab_auto_start and colab_manager.status().state != "ready":
            raise RuntimeError(
                "Google Colab GPU is stopped and auto-start is off. Start Colab from the dashboard or Settings first."
            )
        endpoint = colab_manager.ensure_ready(progress_callback=progress_callback)
        model_name = model or endpoint.model_name
        provider = LLMProvider(
            api_keys=[],
            model_name=model_name,
            host=endpoint.base_url,
            quota_tracker=None,
            api_mode=endpoint.api_mode,
            target_language=target_language,
            context_length=settings.context_length,
            require_api_key=False,
        )
        return provider, model_name

    runtime_keys: list[RuntimeAPIKey] = key_store.runtime_keys_for_user(owner_user)
    if settings.requires_api_key and not runtime_keys:
        raise RuntimeError(
            "At least one API key is required for Ollama Cloud. Add a key in Settings or switch to a local server / Google Colab."
        )
    model_name = model or settings.ollama_model
    is_cloud = settings.provider_backend == "ollama_cloud"
    host = "https://ollama.com" if is_cloud else settings.ollama_host
    api_mode = "ollama" if is_cloud else ("openai" if settings.api_mode == "openai" else "ollama")
    # Local Ollama defaults to 4096 num_ctx unless specified; raise default to 32k locally.
    effective_ctx = settings.context_length
    if not is_cloud and api_mode == "ollama" and effective_ctx == 0:
        effective_ctx = 32768
    tracker = QuotaTracker(settings, runtime_keys) if runtime_keys else None
    provider = LLMProvider(
        api_keys=runtime_keys,
        model_name=model_name,
        host=host,
        quota_tracker=tracker,
        api_mode=api_mode,
        target_language=target_language,
        context_length=effective_ctx,
        require_api_key=settings.requires_api_key,
    )
    return provider, model_name


def translate_stored_work(
    work_root: Path,
    settings: Settings,
    *,
    formats: Iterable[ExportFormat] = tuple(ExportFormat),
    resume: bool = True,
    model: str | None = None,
    target_language: str | None = None,
    refresh_source: bool = False,
    chapter_limit: int | None = None,
    progress_callback: ProgressCallback | None = None,
    owner_user: UserRecord | None = None,
    key_store: APIKeyStore | None = None,
    pixiv_token_store: PixivTokenStore | None = None,
    colab_manager: ColabManager | None = None,
    slot_pool: TranslationSlotPool | None = None,
) -> tuple[Work, Path]:
    """Translate an already-imported work stored on disk."""
    key_store = key_store or APIKeyStore(settings)
    pixiv_token_store = pixiv_token_store or PixivTokenStore(
        settings, key_store.user_store
    )
    work = _load_existing_work(work_root)
    if not work:
        raise RuntimeError(f"Work not found at {work_root}")

    if refresh_source:
        work, work_root = import_url_to_library(
            work.source_url,
            settings,
            output=work_root.parent,
            chapter_limit=chapter_limit,
            progress_callback=progress_callback,
            owner_user=owner_user,
            pixiv_token_store=pixiv_token_store,
            preserve_translations=resume,
        )

    lang = normalize_target_language_name(target_language or settings.target_language)

    def progress(stage: str, current: int, total: int, detail: str) -> None:
        if progress_callback:
            progress_callback(stage, current, total, detail)

    provider, model_name = _resolve_provider(
        settings,
        model=model,
        target_language=lang,
        owner_user=owner_user,
        key_store=key_store,
        colab_manager=colab_manager,
        progress_callback=progress_callback,
    )

    existing_cp = load_checkpoint(work_root) if resume else None
    if existing_cp and (
        existing_cp.target_language.strip().lower() != lang.strip().lower()
    ):
        existing_cp = None

    if not existing_cp:
        work.translated_title = None
        work.translated_description = None
        for chapter in work.chapters:
            chapter.translated_title = None
            chapter.translated_markdown = None

    checkpoint = existing_cp or Checkpoint(
        source_url=work.source_url,
        kind=work.kind,
        pixiv_id=work.pixiv_id,
        original_title=work.original_title,
        target_language=lang,
        model_name=model_name,
    )
    checkpoint.target_language = lang
    checkpoint.model_name = model_name

    def persist_partial(current_work: Work) -> None:
        current_work.target_language = lang
        _write_chapter_files(current_work, work_root)
        save_metadata(work_root, current_work)

    progress("translating", 0, len(work.chapters), f"Starting translation ({lang})")
    try:
        work = translate_work(
            work,
            provider,
            checkpoint,
            checkpoint_callback=lambda cp: save_checkpoint(work_root, cp),
            progress_callback=progress_callback,
            chapter_callback=persist_partial,
            max_workers=settings.translation_concurrency,
            slot_pool=slot_pool,
        )
    finally:
        provider.close()

    work.target_language = lang
    save_checkpoint(work_root, checkpoint)
    _write_chapter_files(work, work_root)

    total = len(work.chapters)
    save_metadata(work_root, work)
    progress("exporting", total, total, "Rendering exports")
    export_work(work, work_root, set(formats))
    progress("completed", total, total, "Done")
    return work, work_root


def translate_url_to_outputs(
    url: str,
    settings: Settings,
    *,
    output: Path | None = None,
    formats: Iterable[ExportFormat] = tuple(ExportFormat),
    resume: bool = False,
    chapter_limit: int | None = None,
    model: str | None = None,
    target_language: str | None = None,
    progress_callback: ProgressCallback | None = None,
    owner_user: UserRecord | None = None,
    key_store: APIKeyStore | None = None,
    pixiv_token_store: PixivTokenStore | None = None,
    colab_manager: ColabManager | None = None,
    slot_pool: TranslationSlotPool | None = None,
) -> tuple[Work, Path]:
    key_store = key_store or APIKeyStore(settings)
    pixiv_token_store = pixiv_token_store or PixivTokenStore(
        settings, key_store.user_store
    )
    if settings.requires_api_key and not key_store.runtime_keys_for_user(owner_user):
        raise RuntimeError(
            "At least one API key is required for Ollama Cloud. Put OLLAMA_API_KEY in .env or add a key in the web dashboard."
        )

    _work, work_root = import_url_to_library(
        url,
        settings,
        output=output,
        chapter_limit=chapter_limit,
        progress_callback=progress_callback,
        owner_user=owner_user,
        pixiv_token_store=pixiv_token_store,
        preserve_translations=resume,
    )
    return translate_stored_work(
        work_root,
        settings,
        formats=formats,
        resume=resume,
        model=model,
        target_language=target_language,
        refresh_source=False,
        progress_callback=progress_callback,
        owner_user=owner_user,
        key_store=key_store,
        pixiv_token_store=pixiv_token_store,
        colab_manager=colab_manager,
        slot_pool=slot_pool,
    )


def _load_existing_work(work_root: Path) -> Work | None:
    metadata_path = work_root / "metadata.json"
    if not metadata_path.exists():
        return None
    return Work.model_validate(json.loads(metadata_path.read_text(encoding="utf-8")))


def _localize_pixiv_assets(work: Work, work_root: Path) -> None:
    replacements = _download_embedded_pixiv_images(work, work_root)
    if not replacements:
        return

    for chapter in work.chapters:
        chapter.source_markdown = _replace_asset_urls(
            chapter.source_markdown, replacements
        )
        if chapter.translated_markdown:
            chapter.translated_markdown = _replace_asset_urls(
                chapter.translated_markdown, replacements
            )


def _download_embedded_pixiv_images(work: Work, work_root: Path) -> dict[str, str]:
    image_urls = {
        url
        for chapter in work.chapters
        for url in _extract_pixiv_image_urls(chapter.source_markdown)
    }
    if not image_urls:
        return {}

    assets_dir = work_root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    replacements: dict[str, str] = {}

    with httpx.Client(
        timeout=30.0,
        headers={
            "Referer": "https://www.pixiv.net/",
            "User-Agent": "Mozilla/5.0 Fableport/0.1.0",
        },
    ) as client:
        for url in sorted(image_urls):
            target = assets_dir / _asset_filename_for_url(url)
            try:
                if not target.exists():
                    response = client.get(url)
                    response.raise_for_status()
                    target.write_bytes(response.content)
                replacements[url] = target.relative_to(work_root).as_posix()
            except httpx.HTTPError:
                continue  # Keep the remote URL if the download fails.
    return replacements


def _extract_pixiv_image_urls(markdown: str) -> list[str]:
    return [
        match.group(1)
        for match in re.finditer(
            r"!\[[^\]]*\]\((https://i\.pximg\.net[^)]+)\)", markdown
        )
    ]


def _replace_asset_urls(markdown: str, replacements: dict[str, str]) -> str:
    for old, new in replacements.items():
        markdown = markdown.replace(old, new)
    return markdown


def _asset_filename_for_url(url: str) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix or ".bin"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"embedded-{digest}{suffix}"
