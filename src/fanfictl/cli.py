from __future__ import annotations

import json
from pathlib import Path

import typer

from fanfictl.config import Settings
from fanfictl.models import ExportFormat
from fanfictl.workflow import fetch_work_from_url, translate_url_to_outputs


app = typer.Typer(help="Translate public Pixiv fanfiction into English")


@app.command()
def info(url: str) -> None:
    work = fetch_work_from_url(url)

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


@app.command()
def translate(
    url: str,
    target: str = typer.Option("en", help="Target language code"),
    output: Path | None = typer.Option(None, help="Output directory"),
    format: list[ExportFormat] = typer.Option(
        [ExportFormat.MD, ExportFormat.TXT, ExportFormat.HTML, ExportFormat.EPUB],
        "--format",
        help="Export format(s)",
    ),
    resume: bool = typer.Option(False, help="Resume from checkpoint if present"),
    chapter_limit: int | None = typer.Option(
        None, help="Limit number of chapters for testing"
    ),
    model: str | None = typer.Option(None, help="Override model name"),
) -> None:
    if target.lower() != "en":
        raise typer.BadParameter("v1 only supports English output")

    settings = Settings()
    try:
        work, work_root = translate_url_to_outputs(
            url,
            settings,
            output=output,
            formats=format,
            resume=resume,
            chapter_limit=chapter_limit,
            model=model,
            progress_callback=lambda _stage, _current, _total, detail: typer.echo(
                detail
            ),
        )
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"Done: {work_root}")


if __name__ == "__main__":
    app()
