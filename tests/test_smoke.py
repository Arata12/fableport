from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fanfictl.content import normalize_pixiv_text_to_markdown
from fanfictl.exporters import (
    build_combined_markdown,
    write_epub,
    write_html,
    write_markdown,
    write_text,
)
from fanfictl.models import Chapter, Checkpoint, Work, WorkKind
from fanfictl.pixiv import parse_pixiv_url
from fanfictl.storage import ensure_work_dirs, load_checkpoint, save_checkpoint
from fanfictl.translate import split_markdown_into_chunks, translate_work


class FakeProvider:
    def translate_title(self, original_title: str) -> str:
        return f"EN {original_title}"

    def translate_chunk(self, chunk: str, previous_context: str | None = None) -> str:
        return f"EN:\n{chunk.strip()}"


class SmokeTests(unittest.TestCase):
    def test_parse_pixiv_url(self) -> None:
        novel = parse_pixiv_url("https://www.pixiv.net/novel/show.php?id=27402134")
        self.assertEqual(novel.kind, "novel")
        self.assertEqual(novel.pixiv_id, 27402134)

        series = parse_pixiv_url("https://www.pixiv.net/novel/series/11824916")
        self.assertEqual(series.kind, "series")
        self.assertEqual(series.pixiv_id, 11824916)

    def test_markdown_normalization(self) -> None:
        markdown = normalize_pixiv_text_to_markdown(
            "[chapter:Start]\nHello\n[newpage]\n[[jumpuri:Pixiv > https://www.pixiv.net]]\n[[rb:漢字 > かんじ]]",
            chapter_title="Title",
        )
        self.assertIn("# Title", markdown)
        self.assertIn("## Start", markdown)
        self.assertIn("[Pixiv](https://www.pixiv.net)", markdown)
        self.assertIn("<ruby>漢字<rt>かんじ</rt></ruby>", markdown)

    def test_translate_pipeline_and_exports(self) -> None:
        work = Work(
            kind=WorkKind.NOVEL,
            pixiv_id=123,
            source_url="https://example.com",
            original_title="Original",
            author_name="Author",
            chapters=[
                Chapter(
                    position=1,
                    pixiv_novel_id=123,
                    original_title="Chapter 1",
                    source_markdown="# Chapter 1\n\nHello world.\n\nAnother paragraph.\n",
                )
            ],
        )

        checkpoint = Checkpoint(
            source_url=work.source_url,
            kind=work.kind,
            pixiv_id=work.pixiv_id,
            original_title=work.original_title,
            model_name="fake-model",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = ensure_work_dirs(Path(tmp), work)
            translated = translate_work(
                work,
                FakeProvider(),
                checkpoint,
                checkpoint_callback=lambda cp: save_checkpoint(root, cp),
            )
            combined = build_combined_markdown(translated)

            self.assertIn("EN Original", translated.translated_title or "")
            self.assertIn("EN:\n# Chapter 1", combined)
            self.assertIsNotNone(load_checkpoint(root))

            write_markdown(root / "translated.md", combined)
            write_text(root / "translated.txt", combined)
            write_html(
                root / "translated.html",
                combined,
                translated.translated_title or translated.original_title,
            )
            write_epub(root / "translated.epub", translated)

            self.assertTrue((root / "translated.md").exists())
            self.assertTrue((root / "translated.txt").exists())
            self.assertTrue((root / "translated.html").exists())
            self.assertTrue((root / "translated.epub").exists())

    def test_chunking(self) -> None:
        chunks = split_markdown_into_chunks("para1\n\npara2\n\npara3", max_chars=8)
        self.assertEqual(len(chunks), 3)


if __name__ == "__main__":
    unittest.main()
