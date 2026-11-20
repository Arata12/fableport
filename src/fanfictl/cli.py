from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import typer

from fanfictl.config import Settings
from fanfictl.content import normalize_target_language_name
from fanfictl.models import ExportFormat
from fanfictl.workflow import (
    fetch_work_from_url,
    import_url_to_library,
    translate_url_to_outputs,
)


app = typer.Typer(help="Import and translate Pixiv fanfiction with a hosted reader")


@app.command()
def info(url: str) -> None:
    try:
        work = fetch_work_from_url(url)
    except (RuntimeError, ValueError) as exc:
        _fail(exc)

    typer.echo(
        json.dumps(
            {
                "kind": work.kind.value,
                "pixiv_id": work.pixiv_id,
                "title": work.original_title,
                "author": work.author_name,
                "chapters": len(work.chapters),
                "language": work.original_language,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command(name="import")
def import_work(
    url: str,
    output: Path | None = typer.Option(None, help="Output directory"),
    chapter_limit: int | None = typer.Option(
        None, help="Limit number of chapters for testing"
    ),
) -> None:
    """Import a Pixiv novel or series into the library without translating it."""
    settings = Settings()
    try:
        _work, work_root = import_url_to_library(
            url,
            settings,
            output=output,
            chapter_limit=chapter_limit,
            progress_callback=lambda _stage, _current, _total, detail: typer.echo(
                detail
            ),
        )
    except (RuntimeError, ValueError) as exc:
        _fail(exc)

    typer.echo(f"Imported: {work_root}")


@app.command()
def translate(
    url: str,
    target: str | None = typer.Option(
        None, help="Target language (e.g. English, Spanish, en, es)"
    ),
    output: Path | None = typer.Option(None, help="Output directory"),
    format: list[ExportFormat] = typer.Option(
        list(ExportFormat),
        "--format",
        help="Export format(s)",
    ),
    resume: bool = typer.Option(False, help="Resume from checkpoint if present"),
    chapter_limit: int | None = typer.Option(
        None, help="Limit number of chapters for testing"
    ),
    model: str | None = typer.Option(None, help="Override model name"),
    host: str | None = typer.Option(None, help="Override LLM host URL"),
    api_mode: str | None = typer.Option(
        None, help="Override API mode ('ollama' or 'openai')"
    ),
) -> None:
    settings = Settings()
    if host:
        settings.ollama_host = host.rstrip("/")
    if api_mode:
        if api_mode.lower() not in {"ollama", "openai"}:
            raise typer.BadParameter("api_mode must be 'ollama' or 'openai'")
        settings.api_mode = api_mode.lower()
    target_language = normalize_target_language_name(target or settings.target_language)

    try:
        _work, work_root = translate_url_to_outputs(
            url,
            settings,
            output=output,
            formats=format,
            resume=resume,
            chapter_limit=chapter_limit,
            model=model,
            target_language=target_language,
            progress_callback=lambda _stage, _current, _total, detail: typer.echo(
                detail
            ),
        )
    except (RuntimeError, ValueError) as exc:
        _fail(exc)

    typer.echo(f"Done: {work_root}")


def _fail(exc: Exception) -> NoReturn:
    typer.secho(str(exc), fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2) from exc


if __name__ == "__main__":
    app()
