from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Literal
from urllib.parse import urlparse

import httpx

from fanfictl.content import (
    language_code_for,
    normalize_target_language_name,
    normalize_translated_text,
)
from fanfictl.keystore import RuntimeAPIKey
from fanfictl.models import Checkpoint, CheckpointChapter, Work
from fanfictl.quota import QuotaTracker


def _dialogue_guidance(target_language: str) -> str:
    code = language_code_for(target_language)
    if code == "en":
        return 'Use standard English double quotation marks (") for dialogue.'
    if code == "es":
        return (
            "Use natural Spanish literary dialogue punctuation (such as em dashes — "
            'or standard quotes) consistently throughout the story.'
        )
    return f"Use natural, standard literary dialogue punctuation for {target_language} consistently."


def _style_guidance(target_language: str) -> str:
    return (
        "Preserve the original meaning and emotional nuance. Maintain distinct character voices based only on the source text itself; "
        "do not rely on outside canon knowledge and do not force exaggerated characterization when the source is neutral. "
        f"Avoid overly literal phrasing if it sounds unnatural in {target_language}. "
        "Lightly adapt culture-specific expressions when needed to preserve tone rather than wording. "
        f"Keep dialogue sounding like real spoken {target_language}. Preserve subtle ambiguity and subtext without over-explaining. "
        "Keep the narration smooth and readable, like a professionally translated novel. "
        "Do not add new content, interpretations, embellishment, or dramatization beyond the source."
    )


def build_title_system(target_language: str = "English") -> str:
    lang = normalize_target_language_name(target_language)
    return (
        f"You translate Pixiv fanfiction titles into natural {lang}. Preserve meaning, tone, and emotional nuance, "
        f"but avoid stiff literal phrasing when it sounds unnatural in {lang}. Return only the translated title."
    )


def build_description_system(target_language: str = "English") -> str:
    lang = normalize_target_language_name(target_language)
    return (
        f"You translate fanfiction summaries into natural {lang}. "
        f"{_style_guidance(lang)} "
        "Keep the result concise and readable. "
        f"{_dialogue_guidance(lang)} Return only the translated summary."
    )


def build_prose_system(target_language: str = "English") -> str:
    lang = normalize_target_language_name(target_language)
    return (
        f"You are translating Japanese fanfiction prose into natural, fluent {lang}. "
        f"{_style_guidance(lang)} "
        "Preserve all meaning, markdown structure, scene breaks, headings, links, and inline formatting exactly. "
        f"Do not summarize, censor, omit, or simplify content. {_dialogue_guidance(lang)} "
        "Return only the translated markdown."
    )


# Kept for backwards compatibility.
TITLE_SYSTEM = build_title_system("English")
TRANSLATION_STYLE_GUIDANCE = _style_guidance("English")
DESCRIPTION_SYSTEM = build_description_system("English")
PROSE_SYSTEM = build_prose_system("English")

# Only the tail of the previous translated chunk is sent as context: enough to
# keep character names, pronouns, and voice consistent across long chapters.
CONTEXT_CHARS = 3000

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", flags=re.DOTALL | re.IGNORECASE)


def _strip_reasoning_tags(text: str) -> str:
    """Remove <think>...</think> wrappers if a reasoning-capable local model emits them."""
    cleaned = _THINK_BLOCK_RE.sub("", text).strip()
    return cleaned or text.strip()


def _is_ollama_cloud_host(host: str) -> bool:
    hostname = (urlparse(host).hostname or "").lower()
    return hostname == "ollama.com" or hostname.endswith(".ollama.com")


def _chat_endpoint_for(host: str, api_mode: str) -> str:
    cleaned = host.rstrip("/")
    if api_mode == "openai":
        if cleaned.endswith("/v1"):
            return "/chat/completions"
        if cleaned.endswith("/v1/chat/completions"):
            return ""
        return "/v1/chat/completions"
    if cleaned.endswith("/api"):
        return "/chat"
    if cleaned.endswith("/api/chat"):
        return ""
    return "/api/chat"


def _models_endpoint_for(host: str, api_mode: str) -> str:
    cleaned = host.rstrip("/")
    if api_mode == "openai":
        if cleaned.endswith("/v1"):
            return "/models"
        return "/v1/models"
    if cleaned.endswith("/api"):
        return "/tags"
    return "/api/tags"


class OutputTruncated(RuntimeError):
    pass


@dataclass
class ConnectionCheckResult:
    ok: bool
    message: str
    models: list[str]


def test_llm_connection(
    *,
    host: str,
    api_mode: str,
    model_name: str,
    api_key: str | None = None,
    timeout: float = 15.0,
) -> ConnectionCheckResult:
    cleaned_host = host.rstrip("/")
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    endpoint = _models_endpoint_for(cleaned_host, api_mode)
    try:
        with httpx.Client(
            base_url=cleaned_host, timeout=httpx.Timeout(timeout, connect=10.0)
        ) as client:
            response = client.get(endpoint, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return ConnectionCheckResult(
            ok=False,
            message=f"Could not reach {cleaned_host}{endpoint}: {_error_text(exc)}",
            models=[],
        )

    models: list[str] = []
    if api_mode == "openai":
        for item in payload.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
    else:
        for item in payload.get("models") or []:
            if isinstance(item, dict) and item.get("name"):
                models.append(str(item["name"]))

    if models and model_name and model_name not in models:
        preview = ", ".join(models[:5])
        suffix = f" (+{len(models) - 5} more)" if len(models) > 5 else ""
        return ConnectionCheckResult(
            ok=True,
            message=(
                f"Connected to {cleaned_host}, but model '{model_name}' was not listed. "
                f"Available: {preview}{suffix}"
            ),
            models=models,
        )
    if models:
        return ConnectionCheckResult(
            ok=True,
            message=f"Connected to {cleaned_host} ({len(models)} model(s) available; '{model_name}' ready).",
            models=models,
        )
    return ConnectionCheckResult(
        ok=True,
        message=f"Connected to {cleaned_host} ({api_mode} API).",
        models=[],
    )


class LLMProvider:
    """Unified translation provider supporting both Ollama native (/api/chat)
    and OpenAI-compatible (/v1/chat/completions: vLLM, local Ollama, llama.cpp, etc.).
    """

    def __init__(
        self,
        api_keys: list[RuntimeAPIKey] | None = None,
        model_name: str = "gemma4:31b",
        host: str = "https://ollama.com",
        quota_tracker: QuotaTracker | None = None,
        *,
        api_mode: Literal["ollama", "openai"] = "ollama",
        target_language: str = "English",
        context_length: int = 0,
        require_api_key: bool | None = None,
    ) -> None:
        self.keys = list(api_keys or [])
        self.host = host.rstrip("/")
        self.api_mode = api_mode if api_mode in {"ollama", "openai"} else "ollama"
        self.target_language = normalize_target_language_name(target_language)
        self.context_length = max(0, int(context_length or 0))
        if require_api_key is None:
            require_api_key = _is_ollama_cloud_host(self.host)
        if require_api_key and not self.keys:
            raise RuntimeError("OLLAMA_API_KEY is required for translation")
        self.model_name = model_name
        self.quota_tracker = quota_tracker if self.keys else None
        self._endpoint = _chat_endpoint_for(self.host, self.api_mode)
        # Long chapters can take minutes to generate; httpx.Client is thread-safe.
        self._http = httpx.Client(
            base_url=self.host, timeout=httpx.Timeout(600.0, connect=30.0)
        )

    def close(self) -> None:
        self._http.close()

    def translate_title(self, original_title: str) -> str:
        lang = getattr(self, "target_language", "English")
        return normalize_translated_text(
            self._generate(
                system_instruction=build_title_system(lang),
                prompt=f"Translate this title into natural {lang}: {original_title}",
            ).strip(),
            lang,
        )

    def translate_description(self, description: str) -> str:
        lang = getattr(self, "target_language", "English")
        return normalize_translated_text(
            self._generate(
                system_instruction=build_description_system(lang),
                prompt=f"Translate this summary into natural {lang}:\n\n{description}",
            ).strip(),
            lang,
        )

    def translate_chunk(self, chunk: str, previous_context: str | None = None) -> str:
        lang = getattr(self, "target_language", "English")
        prompt = chunk
        if previous_context:
            prompt = (
                "Previous translated context for consistency (do not translate it again):\n"
                f"{_tail(previous_context, CONTEXT_CHARS)}\n\n"
                "Now translate the next markdown chunk:\n"
                f"{chunk}"
            )
        try:
            text = self._generate(
                system_instruction=build_prose_system(lang), prompt=prompt
            )
        except OutputTruncated:
            # The model hit its output limit: translate the chunk in two halves.
            halves = split_markdown_into_chunks(chunk, max_chars=len(chunk) // 2 + 2)
            if len(halves) < 2:
                raise
            parts: list[str] = []
            for half in halves:
                parts.append(self.translate_chunk(half, previous_context))
                previous_context = parts[-1]
            return "\n\n".join(part.strip() for part in parts) + "\n"
        return normalize_translated_text(text.strip(), lang)

    def _extract_response_text(self, payload: dict) -> str:
        if self.api_mode == "openai":
            choices = payload.get("choices") or []
            if not choices:
                raise RuntimeError("Empty choices list from OpenAI-compatible server")
            choice = choices[0] or {}
            if choice.get("finish_reason") == "length":
                raise OutputTruncated("Model output was cut at the token limit")
            message = choice.get("message") or {}
            content = message.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            text = _strip_reasoning_tags(str(content))
            if not text.strip():
                raise RuntimeError("Empty response from LLM server")
            return text

        if payload.get("done_reason") == "length":
            raise OutputTruncated("Model output was cut at the token limit")
        raw = (payload.get("message") or {}).get("content") or ""
        text = _strip_reasoning_tags(str(raw))
        if not text.strip():
            raise RuntimeError("Empty response from Ollama")
        return text

    def _generate(self, system_instruction: str, prompt: str) -> str:
        last_error: Exception | None = None
        attempts = max(3, len(self.keys) * 2) if self.keys else 3
        for attempt in range(attempts):
            key: RuntimeAPIKey | None = None
            if self.keys:
                # DailyQuotaExceeded from the tracker propagates: retrying is pointless.
                key = (
                    self.quota_tracker.acquire_request_slot()
                    if self.quota_tracker
                    else self.keys[attempt % len(self.keys)]
                )
            headers: dict[str, str] = {}
            if key and key.key:
                headers["Authorization"] = f"Bearer {key.key}"
            body: dict = {
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            }
            if self.api_mode == "ollama" and self.context_length >= 4096:
                body["options"] = {"num_ctx": self.context_length}
            try:
                response = self._http.post(
                    self._endpoint,
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
                return self._extract_response_text(response.json())
            except OutputTruncated:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if key and self.quota_tracker and _looks_like_quota_error(exc):
                    # Cools the key down; the tracker then waits or picks another key.
                    self.quota_tracker.record_quota_error(key.id, _error_text(exc))
                    continue
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Translation failed: {_error_text(last_error)}") from last_error


class OllamaProvider(LLMProvider):
    """Backwards-compatible alias for LLMProvider defaulting to Ollama /api/chat."""

    def __init__(
        self,
        api_keys: list[RuntimeAPIKey] | None = None,
        model_name: str = "gemma4:31b",
        host: str = "https://ollama.com",
        quota_tracker: QuotaTracker | None = None,
        *,
        api_mode: Literal["ollama", "openai"] = "ollama",
        target_language: str = "English",
        require_api_key: bool | None = None,
    ) -> None:
        super().__init__(
            api_keys=api_keys,
            model_name=model_name,
            host=host,
            quota_tracker=quota_tracker,
            api_mode=api_mode,
            target_language=target_language,
            require_api_key=require_api_key,
        )


def _error_text(exc: Exception | None) -> str:
    """Error message including server JSON error body, never the API key."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
    return str(exc)


def _looks_like_quota_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    text = str(exc).lower()
    return "429" in text or "rate limit" in text


def split_markdown_into_chunks(markdown: str, max_chars: int = 8000) -> list[str]:
    """Pack paragraphs into chunks of at most ~max_chars.

    Many Pixiv texts use single line breaks only, so a "paragraph" can be a
    whole chapter; those are split on line breaks instead.
    """
    pieces: list[str] = []
    for paragraph in markdown.split("\n\n"):
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
        else:
            pieces.extend(_pack(paragraph.split("\n"), max_chars, "\n"))
    return [chunk + "\n" for chunk in _pack(pieces, max_chars, "\n\n") if chunk]


def _pack(parts: list[str], max_chars: int, sep: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for part in parts:
        if current and size + len(part) + len(sep) > max_chars:
            chunks.append(sep.join(current).strip())
            current, size = [], 0
        current.append(part)
        size += len(part) + len(sep)
    if current:
        chunks.append(sep.join(current).strip())
    return chunks


def _tail(text: str, max_chars: int) -> str:
    """Last max_chars of text, starting at a paragraph boundary when possible."""
    if len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    boundary = tail.find("\n\n")
    return tail[boundary + 2 :] if boundary != -1 else tail


def _leading_heading(markdown: str) -> str | None:
    match = re.match(r"\s*#\s+(.+)", markdown)
    return match.group(1).strip() if match else None


def translate_work(
    work: Work,
    provider: LLMProvider,
    checkpoint: Checkpoint,
    checkpoint_callback: Callable[[Checkpoint], None] | None = None,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
    chapter_callback: Callable[[Work], None] | None = None,
    max_workers: int = 1,
) -> Work:
    """Translate every chapter (several in parallel), then title and summary.

    Chunks inside a chapter stay sequential so each one gets the previous
    translation as context; chapters are independent, so they run in parallel
    (the QuotaTracker still enforces the per-key RPM/RPD limits).
    """
    total = len(work.chapters)
    lock = threading.Lock()
    stop = threading.Event()
    completed_chapters = 0
    active_chapters: set[int] = set()
    chunk_max_chars = (
        12000 if getattr(provider, "context_length", 0) >= 32768 else 8000
    )

    def report(stage: str, current: int, detail: str) -> None:
        if progress_callback:
            with lock:
                progress_callback(stage, current, total, detail)

    def save() -> None:
        if checkpoint_callback:
            with lock:
                checkpoint_callback(checkpoint)

    def save_chapter() -> None:
        if chapter_callback:
            with lock:
                chapter_callback(work)

    states = {
        chapter.position: checkpoint.chapter_states.setdefault(
            str(chapter.position), CheckpointChapter()
        )
        for chapter in work.chapters
    }

    def translate_chapter(chapter) -> None:
        nonlocal completed_chapters
        state = states[chapter.position]
        chunks = split_markdown_into_chunks(
            chapter.source_markdown, max_chars=chunk_max_chars
        )
        with lock:
            active_chapters.add(chapter.position)
            active_count = len(active_chapters)
            done_now = completed_chapters
        report(
            "chapter",
            done_now,
            f"Translating {active_count} chapter(s) in parallel ({done_now}/{total} done)",
        )
        try:
            previous = state.translated_chunks[-1] if state.translated_chunks else None
            for index in range(len(state.translated_chunks), len(chunks)):
                if stop.is_set():
                    return
                translated = provider.translate_chunk(
                    chunks[index], previous_context=previous
                )
                with lock:
                    state.translated_chunks.append(translated)
                    done_now = completed_chapters
                previous = translated
                report(
                    "chunk",
                    done_now,
                    f"Chapter {chapter.position}: chunk {index + 1}/{len(chunks)} ({done_now}/{total} chapters done)",
                )
                save()

            chapter.translated_markdown = (
                "\n\n".join(state.translated_chunks).strip() + "\n"
            )
            if not state.translated_title:
                # The chapter text starts with "# <title>", so its title is usually
                # already translated: no extra request needed.
                state.translated_title = _leading_heading(
                    chapter.translated_markdown
                ) or provider.translate_title(chapter.original_title)
                save()
            chapter.translated_title = state.translated_title
            save_chapter()
            with lock:
                completed_chapters += 1
                done_now = completed_chapters
                active_chapters.discard(chapter.position)
                active_count = len(active_chapters)
            detail = (
                f"Completed {done_now}/{total} chapters ({active_count} still running)"
                if active_count
                else f"Completed {done_now}/{total} chapters"
            )
            report("chapter", done_now, detail)
        finally:
            with lock:
                active_chapters.discard(chapter.position)

    pool = ThreadPoolExecutor(max_workers=max(1, max_workers))
    try:
        futures = [pool.submit(translate_chapter, chapter) for chapter in work.chapters]
        for future in as_completed(futures):
            future.result()
    except BaseException:
        stop.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    if not checkpoint.translated_title:
        report("title", total, "Translating work title")
        # A single novel's title is its chapter title, already translated.
        same_title = next(
            (
                chapter.translated_title
                for chapter in work.chapters
                if chapter.original_title == work.original_title
                and chapter.translated_title
            ),
            None,
        )
        checkpoint.translated_title = same_title or provider.translate_title(
            work.original_title
        )
        save()
    work.translated_title = checkpoint.translated_title

    if work.description:
        if not checkpoint.translated_description:
            report("summary", total, "Translating synopsis")
            checkpoint.translated_description = provider.translate_description(
                work.description
            )
            save()
        work.translated_description = checkpoint.translated_description

    return work
