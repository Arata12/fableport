from __future__ import annotations

import mimetypes
from pathlib import Path

from ebooklib import epub
from markdown_it import MarkdownIt

from fanfictl.content import markdown_to_text
from fanfictl.models import Work, WorkKind


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


def write_markdown(path: Path, markdown: str) -> None:
    path.write_text(markdown, encoding="utf-8")


def write_text(path: Path, markdown: str) -> None:
    path.write_text(markdown_to_text(markdown), encoding="utf-8")


def write_html(path: Path, markdown: str, title: str) -> None:
    body = MarkdownIt(
        "commonmark", {"html": True, "linkify": True, "breaks": True}
    ).render(markdown)
    document = f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>{title}</title>
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


def write_epub(path: Path, work: Work) -> None:
    book = epub.EpubBook()
    base_dir = path.parent
    title = work.translated_title or work.original_title
    book.set_identifier(str(work.pixiv_id))
    book.set_title(title)
    book.set_language("en")
    book.add_author(work.author_name)

    nav_items = []
    spine = ["nav"]
    md = MarkdownIt("commonmark", {"html": True, "linkify": True, "breaks": True})

    for chapter in work.chapters:
        chapter_title = chapter.translated_title or chapter.original_title
        html = md.render(chapter.translated_markdown or chapter.source_markdown)
        item = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chapter-{chapter.position}.xhtml",
            lang="en",
        )
        item.content = f"<h1>{chapter_title}</h1>{html}"
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
