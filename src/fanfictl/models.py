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


class Work(BaseModel):
    kind: WorkKind
    pixiv_id: int
    source_url: str
    public_id: str | None = None
    original_title: str
    translated_title: str | None = None
    author_name: str
    description: str = ""
    original_language: str | None = None
    chapters: list[Chapter] = Field(default_factory=list)


class CheckpointChapter(BaseModel):
    translated_title: str | None = None
    translated_chunks: list[str] = Field(default_factory=list)


class Checkpoint(BaseModel):
    source_url: str
    kind: WorkKind
    pixiv_id: int
    original_title: str
    translated_title: str | None = None
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
