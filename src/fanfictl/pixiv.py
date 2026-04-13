from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse

import httpx

from fanfictl.content import normalize_pixiv_text_to_markdown
from fanfictl.models import Chapter, ParsedPixivUrl, Work, WorkKind


NOVEL_ID_RE = re.compile(r"/novel/show\.php\?id=(\d+)")
SERIES_ID_RE = re.compile(r"/novel/series/(\d+)")


def parse_pixiv_url(raw_url: str) -> ParsedPixivUrl:
    if raw_url.isdigit():
        return ParsedPixivUrl(
            kind="novel",
            pixiv_id=int(raw_url),
            url=f"https://www.pixiv.net/novel/show.php?id={raw_url}",
        )

    parsed = urlparse(raw_url)
    path = parsed.path
    query = parse_qs(parsed.query)

    if "show.php" in path and "id" in query:
        novel_id = int(query["id"][0])
        return ParsedPixivUrl(
            kind="novel",
            pixiv_id=novel_id,
            url=f"https://www.pixiv.net/novel/show.php?id={novel_id}",
        )

    match = SERIES_ID_RE.search(path)
    if match:
        series_id = int(match.group(1))
        return ParsedPixivUrl(
            kind="series",
            pixiv_id=series_id,
            url=f"https://www.pixiv.net/novel/series/{series_id}",
        )

    match = NOVEL_ID_RE.search(raw_url)
    if match:
        novel_id = int(match.group(1))
        return ParsedPixivUrl(
            kind="novel",
            pixiv_id=novel_id,
            url=f"https://www.pixiv.net/novel/show.php?id={novel_id}",
        )

    raise ValueError("Unsupported Pixiv novel URL")


class PixivClient:
    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=30.0,
            headers={
                "Referer": "https://www.pixiv.net/",
                "User-Agent": "Mozilla/5.0 fanfictl/0.1.0",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _get_json(self, path: str) -> dict:
        response = self._client.get(f"https://www.pixiv.net{path}")
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(
                payload.get("message") or f"Pixiv request failed for {path}"
            )
        return payload["body"]

    @staticmethod
    def _check_restrictions(body: dict) -> None:
        if int(body.get("restrict", 0)) != 0 or int(body.get("xRestrict", 0)) != 0:
            raise RuntimeError("Restricted Pixiv content is not supported in v1")

    def fetch_novel_work(self, novel_id: int, source_url: str) -> Work:
        body = self._get_json(f"/ajax/novel/{novel_id}")
        self._check_restrictions(body)

        chapter = Chapter(
            position=1,
            pixiv_novel_id=int(body["id"]),
            original_title=body["title"],
            description=_normalize_description(body.get("description", "")),
            source_markdown=normalize_pixiv_text_to_markdown(
                body.get("content", ""), chapter_title=body["title"]
            ),
        )
        return Work(
            kind=WorkKind.NOVEL,
            pixiv_id=int(body["id"]),
            source_url=source_url,
            original_title=body["title"],
            author_name=body.get("userName", "Unknown"),
            description=_normalize_description(body.get("description", "")),
            original_language=body.get("language"),
            chapters=[chapter],
        )

    def fetch_series_work(self, series_id: int, source_url: str) -> Work:
        meta = self._get_json(f"/ajax/novel/series/{series_id}")
        self._check_restrictions(meta)

        chapters_meta = self._fetch_series_content(series_id)
        chapters: list[Chapter] = []
        for position, chapter_meta in enumerate(chapters_meta, start=1):
            chapter_body = self._get_json(f"/ajax/novel/{chapter_meta['id']}")
            self._check_restrictions(chapter_body)
            chapters.append(
                Chapter(
                    position=position,
                    pixiv_novel_id=int(chapter_body["id"]),
                    original_title=chapter_body["title"],
                    description=_normalize_description(
                        chapter_body.get("description", "")
                    ),
                    source_markdown=normalize_pixiv_text_to_markdown(
                        chapter_body.get("content", ""),
                        chapter_title=chapter_body["title"],
                    ),
                )
            )

        return Work(
            kind=WorkKind.SERIES,
            pixiv_id=int(meta["id"]),
            source_url=source_url,
            original_title=meta["title"],
            author_name=meta.get("userName", "Unknown"),
            description=_normalize_description(meta.get("caption", "")),
            original_language=meta.get("language"),
            chapters=chapters,
        )

    def _fetch_series_content(self, series_id: int) -> list[dict]:
        last_order = 0
        items: list[dict] = []

        while True:
            body = self._get_json(
                f"/ajax/novel/series_content/{series_id}?limit=30&last_order={last_order}&order_by=asc"
            )
            page_items = body.get("page", {}).get("seriesContents", [])
            if not page_items:
                break
            items.extend(page_items)
            if len(page_items) < 30:
                break
            last_order = int(
                page_items[-1]
                .get("series", {})
                .get("contentOrder", last_order + len(page_items))
            )

        items.sort(key=lambda item: int(item.get("series", {}).get("contentOrder", 0)))
        return items


def _normalize_description(value: str) -> str:
    value = html.unescape(value)
    value = value.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    return value.strip()
