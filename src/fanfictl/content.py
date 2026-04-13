from __future__ import annotations

import re


RUBY_RE = re.compile(r"\[\[rb:(.*?)\s*>\s*(.*?)\]\]")
JUMP_URI_RE = re.compile(r"\[\[jumpuri:(.*?)\s*>\s*(.*?)\]\]")
CHAPTER_RE = re.compile(r"\[chapter:(.*?)\]")
JUMP_RE = re.compile(r"\[jump:(\d+)\]")


def normalize_pixiv_text_to_markdown(
    text: str, chapter_title: str | None = None
) -> str:
    content = text.replace("\r\n", "\n")
    content = RUBY_RE.sub(
        lambda m: f"<ruby>{m.group(1).strip()}<rt>{m.group(2).strip()}</rt></ruby>",
        content,
    )
    content = JUMP_URI_RE.sub(
        lambda m: f"[{m.group(1).strip()}]({m.group(2).strip()})", content
    )
    content = CHAPTER_RE.sub(lambda m: f"\n\n## {m.group(1).strip()}\n\n", content)
    content = content.replace("[newpage]", "\n\n---\n\n")
    content = JUMP_RE.sub(
        lambda m: f"[Jump to section {m.group(1)}](#jump-{m.group(1)})", content
    )
    content = _collapse_whitespace(content).strip()

    if chapter_title:
        return f"# {chapter_title}\n\n{content}\n"
    return content + "\n"


def markdown_to_text(markdown: str) -> str:
    text = re.sub(r"^#{1,6}\s*", "", markdown, flags=re.MULTILINE)
    text = text.replace("---", "\n")
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 (\2)", text)
    text = re.sub(r"<ruby>(.*?)<rt>(.*?)</rt></ruby>", r"\1 (\2)", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


TRANSLATED_DOUBLE_QUOTES = str.maketrans(
    {
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "〝": '"',
        "〞": '"',
        "＂": '"',
        "«": '"',
        "»": '"',
        "‹": '"',
        "›": '"',
    }
)


def normalize_translated_english_text(text: str) -> str:
    value = text.translate(TRANSLATED_DOUBLE_QUOTES)
    value = re.sub(r'"{2,}', '"', value)
    value = re.sub(
        r"^[ \t]*[—–―-][ \t]*(.+)$",
        _normalize_dialogue_dash_line,
        value,
        flags=re.MULTILINE,
    )
    return value


def _normalize_dialogue_dash_line(match: re.Match[str]) -> str:
    body = match.group(1).strip()
    if not body:
        return match.group(0)
    attribution = re.match(r"^(.*?)(?:\s+[—–―-]\s+)(.+)$", body)
    if attribution:
        return f'"{attribution.group(1).strip()}" {attribution.group(2).strip()}'
    return f'"{body}"'


def _collapse_whitespace(text: str) -> str:
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text
