from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from fanfictl.config import Settings
from fanfictl.jobs import JobManager
from fanfictl.library import (
    get_work_by_public_id,
    get_work_by_root_name,
    list_works,
    output_filename,
    render_chapter_html,
    render_work_html,
)
from fanfictl.models import ExportFormat


PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if (
        settings.uses_default_admin_credentials
        and "localhost" not in settings.app_base_url
        and "127.0.0.1" not in settings.app_base_url
    ):
        raise RuntimeError(
            "Refusing to start with default admin credentials on a non-local APP_BASE_URL."
        )
    if (
        settings.app_secret_key == "change-me-secret"
        and "localhost" not in settings.app_base_url
        and "127.0.0.1" not in settings.app_base_url
    ):
        raise RuntimeError(
            "Refusing to start with the default APP_SECRET_KEY on a non-local APP_BASE_URL."
        )
    app = FastAPI(title="fanfictl")
    app.add_middleware(SessionMiddleware, secret_key=settings.app_secret_key)
    app.mount(
        "/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static"
    )
    app.state.settings = settings
    app.state.jobs = JobManager(settings)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if is_admin(request):
            return RedirectResponse("/dashboard", status_code=303)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str | None = None):
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {
                "error": error,
                "public": True,
                "title": "Admin access",
            },
        )

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        if username == settings.admin_username and password == settings.admin_password:
            request.session["is_admin"] = True
            request.session["username"] = username
            return RedirectResponse("/dashboard", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Wrong credentials",
                "public": True,
                "title": "Admin access",
            },
            status_code=400,
        )

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        redirect = require_admin(request)
        if redirect:
            return redirect
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "title": "Dashboard",
                "jobs": app.state.jobs.store.list_recent(),
                "works": list_works(settings.output_dir),
                "using_default_admin": settings.uses_default_admin_credentials,
                "base_url": settings.app_base_url,
            },
        )

    @app.post("/submit")
    def submit(
        request: Request,
        source_url: str = Form(...),
        resume: str | None = Form(None),
        chapter_limit: int | None = Form(None),
        export_md: str | None = Form(None),
        export_txt: str | None = Form(None),
        export_html: str | None = Form(None),
        export_epub: str | None = Form(None),
    ):
        redirect = require_admin(request)
        if redirect:
            return redirect
        formats = []
        if export_md:
            formats.append(ExportFormat.MD)
        if export_txt:
            formats.append(ExportFormat.TXT)
        if export_html:
            formats.append(ExportFormat.HTML)
        if export_epub:
            formats.append(ExportFormat.EPUB)
        if not formats:
            formats = [
                ExportFormat.MD,
                ExportFormat.TXT,
                ExportFormat.HTML,
                ExportFormat.EPUB,
            ]
        job = app.state.jobs.start_job(
            source_url,
            resume=bool(resume),
            chapter_limit=chapter_limit,
            formats=formats,
            model=None,
        )
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        redirect = require_admin(request)
        if redirect:
            return redirect
        job = app.state.jobs.store.get(job_id)
        if not job:
            return TEMPLATES.TemplateResponse(
                request, "not_found.html", {"title": "Job not found"}, status_code=404
            )
        work_entry = (
            get_work_by_root_name(settings.output_dir, job.work_root_name)
            if job.work_root_name
            else None
        )
        return TEMPLATES.TemplateResponse(
            request,
            "job.html",
            {
                "title": f"Job {job.id}",
                "job": job,
                "work_entry": work_entry,
                "refresh": job.status in {"queued", "running"},
            },
        )

    @app.get("/works/{root_name}", response_class=HTMLResponse)
    def work_detail(request: Request, root_name: str):
        redirect = require_admin(request)
        if redirect:
            return redirect
        entry = get_work_by_root_name(settings.output_dir, root_name)
        if not entry:
            return TEMPLATES.TemplateResponse(
                request, "not_found.html", {"title": "Work not found"}, status_code=404
            )
        return TEMPLATES.TemplateResponse(
            request,
            "work_detail.html",
            {
                "title": entry.work.translated_title or entry.work.original_title,
                "entry": entry,
            },
        )

    @app.get("/read/{token_slug}", response_class=HTMLResponse)
    def read_work(request: Request, token_slug: str):
        public_id = token_slug.split("-", 1)[0]
        entry = get_work_by_public_id(settings.output_dir, public_id)
        if not entry:
            return TEMPLATES.TemplateResponse(
                request,
                "not_found.html",
                {"title": "Work not found", "public": True},
                status_code=404,
            )
        if entry.work.kind.value == "series":
            return TEMPLATES.TemplateResponse(
                request,
                "reader_series.html",
                {
                    "title": entry.work.translated_title or entry.work.original_title,
                    "entry": entry,
                    "public": True,
                },
            )
        return TEMPLATES.TemplateResponse(
            request,
            "reader.html",
            {
                "title": entry.work.translated_title or entry.work.original_title,
                "entry": entry,
                "body_html": render_work_html(entry.work),
                "public": True,
            },
        )

    @app.get("/read/{token_slug}/{chapter_no}", response_class=HTMLResponse)
    def read_chapter(request: Request, token_slug: str, chapter_no: int):
        public_id = token_slug.split("-", 1)[0]
        entry = get_work_by_public_id(settings.output_dir, public_id)
        if not entry or chapter_no < 1 or chapter_no > len(entry.work.chapters):
            return TEMPLATES.TemplateResponse(
                request,
                "not_found.html",
                {"title": "Chapter not found", "public": True},
                status_code=404,
            )
        chapter = entry.work.chapters[chapter_no - 1]
        return TEMPLATES.TemplateResponse(
            request,
            "reader_chapter.html",
            {
                "title": chapter.translated_title or chapter.original_title,
                "entry": entry,
                "chapter": chapter,
                "chapter_no": chapter_no,
                "body_html": render_chapter_html(entry.work, chapter_no),
                "public": True,
            },
        )

    @app.get("/download/{public_id}/{fmt}")
    def download(public_id: str, fmt: str):
        entry = get_work_by_public_id(settings.output_dir, public_id)
        if not entry:
            return RedirectResponse("/", status_code=303)
        try:
            export_format = ExportFormat(fmt)
        except ValueError:
            return RedirectResponse(entry.public_url_path, status_code=303)
        filename = output_filename(entry.work, export_format)
        path = entry.root / filename
        if not path.exists():
            return RedirectResponse(entry.public_url_path, status_code=303)
        from fastapi.responses import FileResponse

        return FileResponse(path, filename=filename)

    return app


def is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def require_admin(request: Request):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=303)
    return None


def serve() -> None:
    settings = Settings()
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    serve()
