from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class WorkKind(str, Enum):
    NOVEL = "novel"
    SERIES = "series"


class Chapter(BaseModel):
    position: int
    pixiv_novel_id: int
    original_title: str
    translated_title: str | None = None
    description: str = ""
    source_markdown: str
    translated_markdown: str | None = None

    @property
    def is_translated(self) -> bool:
        return bool(self.translated_markdown and self.translated_markdown.strip())


class Work(BaseModel):
    kind: WorkKind
    pixiv_id: int
    source_url: str
    public_id: str | None = None
    owner_user_id: int | None = None
    owner_username: str | None = None
    original_title: str
    translated_title: str | None = None
    author_name: str
    description: str = ""
    translated_description: str | None = None
    original_language: str | None = None
    target_language: str | None = None
    chapters: list[Chapter] = Field(default_factory=list)

    @property
    def translated_chapter_count(self) -> int:
        return sum(1 for chapter in self.chapters if chapter.is_translated)

    @property
    def translation_status(self) -> Literal["untranslated", "partial", "translated"]:
        if not self.chapters:
            return "translated" if self.translated_title else "untranslated"
        done = self.translated_chapter_count
        if done == 0:
            return "untranslated"
        if done < len(self.chapters):
            return "partial"
        return "translated"

    @property
    def is_translated(self) -> bool:
        return self.translation_status == "translated"


class CheckpointChapter(BaseModel):
    translated_title: str | None = None
    translated_chunks: list[str] = Field(default_factory=list)


class Checkpoint(BaseModel):
    source_url: str
    kind: WorkKind
    pixiv_id: int
    original_title: str
    translated_title: str | None = None
    translated_description: str | None = None
    target_language: str = "English"
    model_name: str
    chapter_states: dict[str, CheckpointChapter] = Field(default_factory=dict)


class ExportFormat(str, Enum):
    MD = "md"
    TXT = "txt"
    HTML = "html"
    EPUB = "epub"


class ParsedPixivUrl(BaseModel):
    kind: Literal["novel", "series"]
    pixiv_id: int
    url: str
