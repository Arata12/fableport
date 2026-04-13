from __future__ import annotations

import time
from typing import Callable

from google import genai
from google.genai import types

from fanfictl.content import normalize_translated_english_text
from fanfictl.keystore import RuntimeAPIKey
from fanfictl.models import Checkpoint, CheckpointChapter, Work
from fanfictl.quota import DailyQuotaExceeded, QuotaTracker


TITLE_SYSTEM = "You translate Pixiv fanfiction titles into natural English. Return only the translated title."

DESCRIPTION_SYSTEM = (
    "You translate fanfiction summaries into natural English. Preserve meaning and tone, but keep it concise and readable. "
    'Use standard English double quotation marks (") for dialogue. Return only the translated summary.'
)

PROSE_SYSTEM = (
    "You are translating fanfiction prose into natural English. Preserve all meaning, tone, markdown structure, scene breaks, and links. "
    'Do not summarize, censor, or omit content. Use standard English double quotation marks (") for dialogue consistently. Return only the translated markdown.'
)


class GeminiStudioProvider:
    def __init__(
        self,
        api_keys: list[RuntimeAPIKey],
        model_name: str,
        quota_tracker: QuotaTracker | None = None,
    ) -> None:
        if not api_keys:
            raise RuntimeError("GEMINI_API_KEY is required for translation")
        self.model_name = model_name
        self.keys = api_keys
        self.clients = {item.id: genai.Client(api_key=item.key) for item in api_keys}
        self.quota_tracker = quota_tracker

    def translate_title(self, original_title: str) -> str:
        return normalize_translated_english_text(
            self._generate(
                system_instruction=TITLE_SYSTEM,
                prompt=f"Translate this title into natural English: {original_title}",
            ).strip()
        )

    def translate_description(self, description: str) -> str:
        return normalize_translated_english_text(
            self._generate(
                system_instruction=DESCRIPTION_SYSTEM,
                prompt=f"Translate this summary into natural English:\n\n{description}",
            ).strip()
        )

    def translate_chunk(self, chunk: str, previous_context: str | None = None) -> str:
        prompt = chunk
        if previous_context:
            prompt = (
                "Previous translated context for consistency:\n"
                f"{previous_context}\n\n"
                "Now translate the next markdown chunk:\n"
                f"{chunk}"
            )
        return normalize_translated_english_text(
            self._generate(system_instruction=PROSE_SYSTEM, prompt=prompt).strip()
        )

    def _generate(self, system_instruction: str, prompt: str) -> str:
        last_error: Exception | None = None
        selected_key = self.keys[0]
        for attempt in range(max(3, len(self.keys) * 2)):
            try:
                if self.quota_tracker:
                    selected_key = self.quota_tracker.acquire_request_slot()
                response = self.clients[selected_key.id].models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction
                    ),
                )
                if not response.text:
                    raise RuntimeError("Empty response from Gemini API")
                return response.text
            except DailyQuotaExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if self.quota_tracker and _looks_like_quota_error(exc):
                    self.quota_tracker.record_quota_error(selected_key.id, str(exc))
                    continue
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Translation failed: {last_error}") from last_error


def _looks_like_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "429" in text
        or "quota" in text
        or "rate limit" in text
        or "resource_exhausted" in text
    )


def split_markdown_into_chunks(markdown: str, max_chars: int = 5000) -> list[str]:
    paragraphs = markdown.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for paragraph in paragraphs:
        if current and current_len + len(paragraph) + 2 > max_chars:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_len = 0

        current.append(paragraph)
        current_len += len(paragraph) + 2

    if current:
        chunks.append("\n\n".join(current).strip())
    return [chunk + "\n" for chunk in chunks if chunk.strip()]


def translate_work(
    work: Work,
    provider: GeminiStudioProvider,
    checkpoint: Checkpoint,
    checkpoint_callback: Callable[[Checkpoint], None] | None = None,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
) -> Work:
    if not checkpoint.translated_title:
        if progress_callback:
            progress_callback("title", 0, len(work.chapters), "Translating work title")
        checkpoint.translated_title = provider.translate_title(work.original_title)
        work.translated_title = checkpoint.translated_title
        if checkpoint_callback:
            checkpoint_callback(checkpoint)
    else:
        work.translated_title = checkpoint.translated_title

    if work.description:
        if not checkpoint.translated_description:
            if progress_callback:
                progress_callback(
                    "summary", 0, len(work.chapters), "Translating synopsis"
                )
            checkpoint.translated_description = provider.translate_description(
                work.description
            )
            if checkpoint_callback:
                checkpoint_callback(checkpoint)
        work.translated_description = checkpoint.translated_description

    for chapter in work.chapters:
        state = checkpoint.chapter_states.setdefault(
            str(chapter.position), CheckpointChapter()
        )
        if not state.translated_title:
            if progress_callback:
                progress_callback(
                    "chapter-title",
                    chapter.position,
                    len(work.chapters),
                    f"Translating chapter title {chapter.position}/{len(work.chapters)}",
                )
            state.translated_title = provider.translate_title(chapter.original_title)
            if checkpoint_callback:
                checkpoint_callback(checkpoint)
        chapter.translated_title = state.translated_title

        chunks = split_markdown_into_chunks(chapter.source_markdown)
        previous_context = (
            state.translated_chunks[-1] if state.translated_chunks else None
        )
        start_index = len(state.translated_chunks)

        if progress_callback:
            progress_callback(
                "chapter",
                chapter.position,
                len(work.chapters),
                f"Translating chapter {chapter.position}/{len(work.chapters)}",
            )

        for idx, chunk in enumerate(chunks[start_index:], start=start_index + 1):
            translated = provider.translate_chunk(
                chunk, previous_context=previous_context
            )
            state.translated_chunks.append(translated)
            previous_context = translated
            if progress_callback:
                progress_callback(
                    "chunk",
                    chapter.position,
                    len(work.chapters),
                    f"Translated chunk {idx}/{len(chunks)} in chapter {chapter.position}",
                )
            if checkpoint_callback:
                checkpoint_callback(checkpoint)

        chapter.translated_markdown = (
            "\n\n".join(state.translated_chunks).strip() + "\n"
        )

    return work
