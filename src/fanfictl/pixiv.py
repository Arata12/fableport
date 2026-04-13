from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse

import httpx
from pixivpy3 import AppPixivAPI

from fanfictl.content import normalize_pixiv_text_to_markdown
from fanfictl.models import Chapter, ParsedPixivUrl, Work, WorkKind
from fanfictl.pixiv_tokens import RuntimePixivToken


NOVEL_ID_RE = re.compile(r"/novel/show\.php\?id=(\d+)")
SERIES_ID_RE = re.compile(r"/novel/series/(\d+)")


class PixivAccessError(RuntimeError):
    pass


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
                "User-Agent": "Mozilla/5.0 Fableport/0.1.0",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _get_json(self, path: str) -> dict:
        try:
            response = self._client.get(f"https://www.pixiv.net{path}")
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise PixivAccessError(f"Pixiv request failed for {path}") from exc
        if payload.get("error"):
            raise PixivAccessError(
                payload.get("message") or f"Pixiv request failed for {path}"
            )
        return payload["body"]

    @staticmethod
    def _check_restrictions(body: dict) -> None:
        if int(body.get("restrict", 0)) != 0 or int(body.get("xRestrict", 0)) != 0:
            raise PixivAccessError("This Pixiv work requires authenticated access")

    def fetch_novel_work(self, novel_id: int, source_url: str) -> Work:
        body = self._get_json(f"/ajax/novel/{novel_id}")
        self._check_restrictions(body)
        content = body.get("content")
        if not content:
            raise PixivAccessError(
                "This Pixiv work returned no public text and may require login"
            )
        return _build_novel_work(
            pixiv_id=int(body["id"]),
            source_url=source_url,
            title=body.get("title") or f"Novel {novel_id}",
            author_name=body.get("userName") or "Unknown",
            description=_normalize_description(body.get("description")),
            language=body.get("language"),
            content=content,
        )

    def fetch_series_work(self, series_id: int, source_url: str) -> Work:
        meta = self._get_json(f"/ajax/novel/series/{series_id}")
        self._check_restrictions(meta)

        chapters_meta = self._fetch_series_content(series_id)
        chapters: list[Chapter] = []
        for position, chapter_meta in enumerate(chapters_meta, start=1):
            chapter_body = self._get_json(f"/ajax/novel/{chapter_meta['id']}")
            self._check_restrictions(chapter_body)
            content = chapter_body.get("content")
            if not content:
                raise PixivAccessError(
                    "A chapter in this series returned no public text and may require login"
                )
            chapters.append(
                _build_chapter(
                    position=position,
                    pixiv_novel_id=int(chapter_body.get("id", chapter_meta["id"])),
                    title=chapter_body.get("title") or f"Chapter {position}",
                    description=_normalize_description(chapter_body.get("description")),
                    content=content,
                )
            )

        return Work(
            kind=WorkKind.SERIES,
            pixiv_id=int(meta.get("id", series_id)),
            source_url=source_url,
            original_title=meta.get("title") or f"Series {series_id}",
            author_name=meta.get("userName") or "Unknown",
            description=_normalize_description(meta.get("caption")),
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


class AuthenticatedPixivClient:
    def __init__(self, tokens: list[RuntimePixivToken]) -> None:
        if not tokens:
            raise PixivAccessError("No Pixiv tokens are configured")
        self.tokens = tokens

    def fetch_novel_work(self, novel_id: int, source_url: str) -> Work:
        return self._run_with_tokens(
            lambda api: self._fetch_novel_with_api(api, novel_id, source_url)
        )

    def fetch_series_work(self, series_id: int, source_url: str) -> Work:
        return self._run_with_tokens(
            lambda api: self._fetch_series_with_api(api, series_id, source_url)
        )

    def _run_with_tokens(self, operation):
        last_error: Exception | None = None
        for token in self.tokens:
            api = AppPixivAPI()
            try:
                api.auth(refresh_token=token.refresh_token)
                return operation(api)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        raise PixivAccessError(
            "This Pixiv work requires authenticated access and could not be fetched with the available Pixiv tokens."
        ) from last_error

    def _fetch_novel_with_api(
        self, api: AppPixivAPI, novel_id: int, source_url: str
    ) -> Work:
        detail_result = api.novel_detail(novel_id)
        novel = _attr(detail_result, "novel")
        text_result = api.novel_text(novel_id)
        content = _attr(text_result, "text") or _attr(text_result, "novel_text")
        if not content:
            raise PixivAccessError("Authenticated Pixiv fetch returned no novel text")
        return _build_novel_work(
            pixiv_id=int(_attr(novel, "id", default=novel_id)),
            source_url=source_url,
            title=_attr(novel, "title", default=f"Novel {novel_id}"),
            author_name=_author_name(novel),
            description=_normalize_description(_attr(novel, "caption")),
            language=_attr(novel, "language"),
            content=content,
        )

    def _fetch_series_with_api(
        self, api: AppPixivAPI, series_id: int, source_url: str
    ) -> Work:
        result = api.novel_series(series_id)
        detail = _attr(result, "novel_series_detail")
        novels = list(_attr(result, "novels", default=[]) or [])
        next_url = _attr(result, "next_url")

        while next_url:
            params = {
                key: values[0]
                for key, values in parse_qs(urlparse(next_url).query).items()
            }
            next_result = api.novel_series(**params)
            novels.extend(list(_attr(next_result, "novels", default=[]) or []))
            next_url = _attr(next_result, "next_url")

        chapters: list[Chapter] = []
        for position, novel in enumerate(novels, start=1):
            novel_id = int(_attr(novel, "id", default=0))
            text_result = api.novel_text(novel_id)
            content = _attr(text_result, "text") or _attr(text_result, "novel_text")
            if not content:
                raise PixivAccessError(
                    "Authenticated Pixiv fetch returned no chapter text"
                )
            chapters.append(
                _build_chapter(
                    position=position,
                    pixiv_novel_id=novel_id,
                    title=_attr(novel, "title", default=f"Chapter {position}"),
                    description=_normalize_description(_attr(novel, "caption")),
                    content=content,
                )
            )

        return Work(
            kind=WorkKind.SERIES,
            pixiv_id=int(_attr(detail, "id", default=series_id)),
            source_url=source_url,
            original_title=_attr(detail, "title", default=f"Series {series_id}"),
            author_name=_author_name(detail) or "Unknown",
            description=_normalize_description(_attr(detail, "caption")),
            original_language=_attr(detail, "language"),
            chapters=chapters,
        )


def _build_novel_work(
    *,
    pixiv_id: int,
    source_url: str,
    title: str,
    author_name: str,
    description: str,
    language: str | None,
    content: str,
) -> Work:
    chapter = _build_chapter(
        position=1,
        pixiv_novel_id=pixiv_id,
        title=title,
        description=description,
        content=content,
    )
    return Work(
        kind=WorkKind.NOVEL,
        pixiv_id=pixiv_id,
        source_url=source_url,
        original_title=title,
        author_name=author_name,
        description=description,
        original_language=language,
        chapters=[chapter],
    )


def _build_chapter(
    *,
    position: int,
    pixiv_novel_id: int,
    title: str,
    description: str,
    content: str,
) -> Chapter:
    return Chapter(
        position=position,
        pixiv_novel_id=pixiv_novel_id,
        original_title=title,
        description=description,
        source_markdown=normalize_pixiv_text_to_markdown(content, chapter_title=title),
    )


def _normalize_description(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = value.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    return value.strip()


def _attr(obj, name: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _author_name(obj) -> str:
    user = _attr(obj, "user")
    return (
        _attr(user, "name", default=_attr(obj, "user_name", default="Unknown"))
        or "Unknown"
    )
