from __future__ import annotations

import threading
import unittest

from fanfictl.models import Chapter, Checkpoint, Work, WorkKind
from fanfictl.translate import (
    OllamaProvider,
    OutputTruncated,
    split_markdown_into_chunks,
    translate_work,
)


class CountingProvider:
    """Translates by upper-casing; counts calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def _record(self, kind: str) -> None:
        with self._lock:
            self.calls.append(kind)

    def translate_title(self, original_title: str) -> str:
        self._record("title")
        return original_title.upper()

    def translate_description(self, description: str) -> str:
        self._record("description")
        return description.upper()

    def translate_chunk(self, chunk: str, previous_context: str | None = None) -> str:
        self._record("chunk")
        return chunk.upper()


def make_work(kind: WorkKind, titles: list[str], title: str) -> Work:
    return Work(
        kind=kind,
        pixiv_id=1,
        source_url="https://example.com",
        original_title=title,
        author_name="Author",
        chapters=[
            Chapter(
                position=i,
                pixiv_novel_id=i,
                original_title=t,
                source_markdown=f"# {t}\n\nBody of {t}.\n",
            )
            for i, t in enumerate(titles, start=1)
        ],
    )


def make_checkpoint(work: Work) -> Checkpoint:
    return Checkpoint(
        source_url=work.source_url,
        kind=work.kind,
        pixiv_id=work.pixiv_id,
        original_title=work.original_title,
        model_name="fake",
    )


class TranslateWorkTests(unittest.TestCase):
    def test_novel_needs_no_separate_title_requests(self) -> None:
        work = make_work(WorkKind.NOVEL, ["Title"], "Title")
        provider = CountingProvider()
        result = translate_work(work, provider, make_checkpoint(work))
        self.assertEqual(provider.calls, ["chunk"])
        self.assertEqual(result.translated_title, "TITLE")
        self.assertEqual(result.chapters[0].translated_title, "TITLE")

    def test_series_translates_chapters_in_parallel_and_in_order(self) -> None:
        titles = [f"Chapter {i}" for i in range(1, 6)]
        work = make_work(WorkKind.SERIES, titles, "Series")
        provider = CountingProvider()
        saves: list[int] = []
        result = translate_work(
            work,
            provider,
            make_checkpoint(work),
            checkpoint_callback=lambda cp: saves.append(1),
            max_workers=3,
        )
        # 5 chunks + 1 series title; chapter titles come from the headings.
        self.assertEqual(provider.calls.count("chunk"), 5)
        self.assertEqual(provider.calls.count("title"), 1)
        self.assertEqual(
            [c.translated_title for c in result.chapters], [t.upper() for t in titles]
        )
        self.assertTrue(saves)

    def test_resume_skips_translated_chunks(self) -> None:
        work = make_work(WorkKind.NOVEL, ["Title"], "Title")
        checkpoint = make_checkpoint(work)
        translate_work(work, CountingProvider(), checkpoint)
        provider = CountingProvider()
        translate_work(make_work(WorkKind.NOVEL, ["Title"], "Title"), provider, checkpoint)
        self.assertEqual(provider.calls, [])


class ChunkingTests(unittest.TestCase):
    def test_long_text_without_blank_lines_is_split(self) -> None:
        text = "\n".join(f"Line {i} " + "x" * 90 for i in range(200))
        chunks = split_markdown_into_chunks(text, max_chars=1000)
        self.assertGreater(len(chunks), 10)
        self.assertTrue(all(len(chunk) <= 1001 for chunk in chunks))
        self.assertEqual(
            "".join(chunks).replace("\n", ""), text.replace("\n", "")
        )


class TruncatingProvider(OllamaProvider):
    def __init__(self) -> None:  # no HTTP client
        self.prompts: list[str] = []

    def _generate(self, system_instruction: str, prompt: str) -> str:
        self.prompts.append(prompt)
        if len(prompt) > 600:
            raise OutputTruncated("too long")
        return "translated"


class TruncationTests(unittest.TestCase):
    def test_truncated_output_is_retried_in_halves(self) -> None:
        provider = TruncatingProvider()
        chunk = "\n\n".join("p" * 200 for _ in range(4))
        result = provider.translate_chunk(chunk)
        self.assertEqual(result.count("translated"), 2)


if __name__ == "__main__":
    unittest.main()


class OllamaProviderTests(unittest.TestCase):
    def make_provider(self, handler, keys=("k1",), tracker=None):
        import httpx

        from fanfictl.keystore import RuntimeAPIKey

        provider = OllamaProvider(
            [RuntimeAPIKey(id=k, key=f"secret-{k}", source="env", is_default=True) for k in keys],
            model_name="gemma4:31b",
            quota_tracker=tracker,
        )
        provider._http.close()
        provider._http = httpx.Client(
            base_url="https://ollama.com", transport=httpx.MockTransport(handler)
        )
        return provider

    def test_calls_ollama_cloud_chat_api(self) -> None:
        import json

        import httpx

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["Authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"message": {"role": "assistant", "content": "Hello"}, "done_reason": "stop"},
            )

        provider = self.make_provider(handler)
        self.assertEqual(provider.translate_title("こんにちは"), "Hello")
        self.assertEqual(seen["url"], "https://ollama.com/api/chat")
        self.assertEqual(seen["auth"], "Bearer secret-k1")
        self.assertEqual(seen["body"]["model"], "gemma4:31b")
        self.assertFalse(seen["body"]["stream"])
        self.assertEqual([m["role"] for m in seen["body"]["messages"]], ["system", "user"])

    def test_length_done_reason_counts_as_truncated(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"message": {"content": "partial"}, "done_reason": "length"}
            )

        with self.assertRaises(OutputTruncated):
            self.make_provider(handler)._generate("sys", "prompt")

    def test_429_switches_to_next_key(self) -> None:
        import tempfile
        from pathlib import Path

        import httpx

        from fanfictl.config import Settings
        from fanfictl.keystore import RuntimeAPIKey
        from fanfictl.quota import QuotaTracker

        used = []

        def handler(request: httpx.Request) -> httpx.Response:
            used.append(request.headers["Authorization"])
            if request.headers["Authorization"] == "Bearer secret-k1":
                return httpx.Response(429, json={"error": "usage limit reached"})
            return httpx.Response(200, json={"message": {"content": "ok"}, "done_reason": "stop"})

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp)
            settings.rpm_limit = 0
            settings.rpd_limit = 0
            keys = [RuntimeAPIKey(id=k, key=f"secret-{k}", source="env", is_default=True) for k in ("k1", "k2")]
            tracker = QuotaTracker(settings, keys)
            provider = self.make_provider(handler, keys=("k1", "k2"), tracker=tracker)
            self.assertEqual(provider._generate("sys", "prompt"), "ok")
            self.assertEqual(used, ["Bearer secret-k1", "Bearer secret-k2"])
            self.assertIn("429", tracker.snapshot().keys[0].last_error)
            self.assertTrue(tracker.snapshot().available)


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_openai_compatible_local_no_key_and_spanish_dialogue(self) -> None:
        import json

        import httpx

        from fanfictl.translate import LLMProvider

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "<think>planning</think>—Hola —dijo ella.",
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        provider = LLMProvider(
            api_keys=[],
            model_name="Qwen/Qwen2.5-14B-Instruct-AWQ",
            host="http://127.0.0.1:8001",
            api_mode="openai",
            target_language="Spanish",
        )
        provider._http.close()
        provider._http = httpx.Client(
            base_url="http://127.0.0.1:8001",
            transport=httpx.MockTransport(handler),
        )
        result = provider.translate_chunk("「こんにちは」と彼女は言った。")
        self.assertEqual(seen["url"], "http://127.0.0.1:8001/v1/chat/completions")
        self.assertIsNone(seen["auth"])
        self.assertIn("Spanish", seen["body"]["messages"][0]["content"])
        # Spanish em-dash dialogue is preserved and <think> wrapper is stripped.
        self.assertEqual(result, "—Hola —dijo ella.")

    def test_openai_finish_reason_length_raises_truncated(self) -> None:
        import httpx

        from fanfictl.translate import LLMProvider

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "cut off"},
                            "finish_reason": "length",
                        }
                    ]
                },
            )

        provider = LLMProvider(
            api_keys=[],
            model_name="vllm-model",
            host="http://127.0.0.1:8001/v1",
            api_mode="openai",
        )
        provider._http.close()
        provider._http = httpx.Client(
            base_url="http://127.0.0.1:8001/v1",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(OutputTruncated):
            provider._generate("sys", "prompt")
