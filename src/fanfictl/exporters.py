from __future__ import annotations

import html
import mimetypes
from pathlib import Path

from ebooklib import epub
from markdown_it import MarkdownIt

from fanfictl.content import language_code_for, markdown_to_text
from fanfictl.models import ExportFormat, Work, WorkKind


MARKDOWN = MarkdownIt("commonmark", {"html": True, "linkify": True, "breaks": True})


def output_filename(work: Work, fmt: ExportFormat) -> str:
    stem = "translated" if work.kind == WorkKind.NOVEL else "combined"
    return f"{stem}.{fmt.value}"


def export_work(work: Work, root: Path, formats: set[ExportFormat]) -> None:
    combined = build_combined_markdown(work)
    title = work.translated_title or work.original_title
    lang = language_code_for(work.target_language)
    for fmt in formats:
        path = root / output_filename(work, fmt)
        if fmt == ExportFormat.MD:
            path.write_text(combined, encoding="utf-8")
        elif fmt == ExportFormat.TXT:
            path.write_text(markdown_to_text(combined), encoding="utf-8")
        elif fmt == ExportFormat.HTML:
            write_html(path, combined, title, lang=lang)
        elif fmt == ExportFormat.EPUB:
            write_epub(path, work, lang=lang)


def build_combined_markdown(work: Work) -> str:
    parts = [f"# {work.translated_title or work.original_title}", ""]
    summary = work.translated_description or work.description
    if summary:
        parts.extend([summary, ""])

    for idx, chapter in enumerate(work.chapters):
        if idx > 0:
            parts.extend(["", "---", ""])
        parts.append(chapter.translated_markdown or chapter.source_markdown)

    return "\n".join(parts).strip() + "\n"


def write_html(path: Path, markdown: str, title: str, *, lang: str = "en") -> None:
    body = MARKDOWN.render(markdown)
    document = f"""<!doctype html>
<html lang="{html.escape(lang)}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)}</title>
    <style>
      body {{ max-width: 760px; margin: 2rem auto; padding: 0 1rem; font-family: Georgia, serif; line-height: 1.7; }}
      hr {{ margin: 2rem 0; }}
      ruby rt {{ font-size: 0.7em; }}
    </style>
  </head>
  <body>
    {body}
  </body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def write_epub(path: Path, work: Work, *, lang: str | None = None) -> None:
    book = epub.EpubBook()
    base_dir = path.parent
    title = work.translated_title or work.original_title
    lang_code = lang or language_code_for(work.target_language)
    book.set_identifier(str(work.pixiv_id))
    book.set_title(title)
    book.set_language(lang_code)
    book.add_author(work.author_name)

    nav_items = []
    spine = ["nav"]
    for chapter in work.chapters:
        chapter_title = chapter.translated_title or chapter.original_title
        item = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chapter-{chapter.position}.xhtml",
            lang=lang_code,
        )
        # The chapter markdown already starts with "# <chapter title>".
        item.content = MARKDOWN.render(
            chapter.translated_markdown or chapter.source_markdown
        )
        book.add_item(item)
        nav_items.append(item)
        spine.append(item)

    book.toc = tuple(nav_items)
    book.spine = spine

    for asset_path in sorted((base_dir / "assets").glob("**/*")):
        if not asset_path.is_file():
            continue
        media_type = (
            mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
        )
        book.add_item(
            epub.EpubItem(
                uid=asset_path.stem,
                file_name=asset_path.relative_to(base_dir).as_posix(),
                media_type=media_type,
                content=asset_path.read_bytes(),
            )
        )

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)
