from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from fanfictl.auth import UserRecord, UserStore
from fanfictl.config import Settings
from fanfictl.jobs import JobManager
from fanfictl.keystore import APIKeyStore
from fanfictl.library import (
    get_work_by_public_id,
    get_work_by_root_name,
    list_works,
    output_filename,
    render_chapter_html,
    render_work_html,
)
from fanfictl.models import ExportFormat
from fanfictl.quota import QuotaTracker


PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if (
        settings.app_secret_key == "change-me-secret"
        and "localhost" not in settings.app_base_url
        and "127.0.0.1" not in settings.app_base_url
    ):
        raise RuntimeError(
            "Refusing to start with the default APP_SECRET_KEY on a non-local APP_BASE_URL."
        )
    app = FastAPI(title="Fableport")
    app.add_middleware(SessionMiddleware, secret_key=settings.app_secret_key)
    app.mount(
        "/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static"
    )
    app.state.settings = settings
    app.state.user_store = UserStore(settings)
    app.state.key_store = APIKeyStore(settings, app.state.user_store)
    app.state.jobs = JobManager(
        settings,
        user_store=app.state.user_store,
        key_store=app.state.key_store,
    )

    def render_dashboard(
        request: Request,
        error: str | None = None,
        status_code: int = 200,
        active_tab: str = "overview",
    ):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        runtime_keys = app.state.key_store.runtime_keys_for_user(user)
        quota = QuotaTracker(settings, runtime_keys).snapshot()
        works = list_works(settings.output_dir)
        my_works = [entry for entry in works if entry.work.owner_user_id == user.id]
        public_works = [entry for entry in works if entry.work.owner_user_id != user.id]
        jobs = (
            app.state.jobs.store.list_recent()
            if user.role == "admin"
            else [
                job
                for job in app.state.jobs.store.list_recent(limit=1000)
                if job.owner_user_id == user.id
            ][:10]
        )
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "title": "Dashboard",
                "current_user": user,
                "jobs": jobs,
                "my_works": my_works,
                "public_works": public_works,
                "using_default_admin": settings.uses_default_admin_credentials,
                "base_url": settings.app_base_url,
                "quota": quota,
                "personal_keys": app.state.key_store.list_personal_keys(user),
                "global_keys": app.state.key_store.list_global_keys(),
                "users": app.state.user_store.list_users()
                if user.role == "admin"
                else [],
                "form_error": error,
                "active_tab": active_tab,
            },
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if current_user(request):
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
                "title": "Sign in",
            },
        )

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        user = app.state.user_store.authenticate(username, password)
        if user:
            request.session["user_id"] = user.id
            request.session["username"] = user.username
            request.session["role"] = user.role
            return RedirectResponse("/dashboard", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Wrong credentials",
                "public": True,
                "title": "Sign in",
            },
            status_code=400,
        )

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        redirect = require_login(request)
        if redirect:
            return redirect
        return render_dashboard(request, active_tab="overview")

    @app.get("/dashboard/library", response_class=HTMLResponse)
    def dashboard_library(request: Request):
        redirect = require_login(request)
        if redirect:
            return redirect
        return render_dashboard(request, active_tab="library")

    @app.get("/dashboard/settings", response_class=HTMLResponse)
    def dashboard_settings(request: Request):
        redirect = require_login(request)
        if redirect:
            return redirect
        return render_dashboard(request, active_tab="settings")

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
        redirect = require_login(request)
        if redirect:
            return redirect
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        quota = QuotaTracker(settings, app.state.key_store.runtime_keys_for_user(user))
        if quota.daily_limit_reached():
            return render_dashboard(
                request,
                error="Daily Gemini request limit reached. Wait until the reset time shown below.",
                status_code=429,
                active_tab="overview",
            )
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
            owner_user=user,
        )
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/keys/personal")
    def add_personal_key(request: Request, api_key: str = Form(...)):
        redirect = require_login(request)
        if redirect:
            return redirect
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        try:
            app.state.key_store.add_user_key(user, api_key)
        except ValueError as exc:
            return render_dashboard(
                request, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/keys/personal/{key_id}/delete")
    def delete_personal_key(request: Request, key_id: str):
        redirect = require_login(request)
        if redirect:
            return redirect
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        app.state.key_store.remove_user_key(user, key_id)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/keys/global")
    def add_global_key(request: Request, api_key: str = Form(...)):
        redirect = require_admin(request)
        if redirect:
            return redirect
        try:
            app.state.key_store.add_global_key(api_key)
        except ValueError as exc:
            return render_dashboard(
                request, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/keys/global/{key_id}/delete")
    def delete_global_key(request: Request, key_id: str):
        redirect = require_admin(request)
        if redirect:
            return redirect
        app.state.key_store.remove_global_key(key_id)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/users")
    def create_user(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        role: str = Form("user"),
    ):
        redirect = require_admin(request)
        if redirect:
            return redirect
        try:
            app.state.user_store.create_user(
                username=username, password=password, role=role
            )
        except ValueError as exc:
            return render_dashboard(
                request, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/account/password")
    def change_password(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        confirm_password: str = Form(...),
    ):
        redirect = require_login(request)
        if redirect:
            return redirect
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if new_password != confirm_password:
            return render_dashboard(
                request,
                error="New password and confirmation do not match.",
                status_code=400,
                active_tab="settings",
            )
        try:
            app.state.user_store.change_password(user, current_password, new_password)
        except ValueError as exc:
            return render_dashboard(
                request, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        redirect = require_login(request)
        if redirect:
            return redirect
        user = current_user(request)
        job = app.state.jobs.store.get(job_id)
        if not job or not user:
            return TEMPLATES.TemplateResponse(
                request, "not_found.html", {"title": "Job not found"}, status_code=404
            )
        if user.role != "admin" and job.owner_user_id != user.id:
            return RedirectResponse("/dashboard", status_code=303)
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
                "current_user": user,
                "job": job,
                "work_entry": work_entry,
                "refresh": job.status in {"queued", "running"},
            },
        )

    @app.get("/works/{root_name}", response_class=HTMLResponse)
    def work_detail(request: Request, root_name: str):
        redirect = require_login(request)
        if redirect:
            return redirect
        user = current_user(request)
        entry = get_work_by_root_name(settings.output_dir, root_name)
        if not entry or not user:
            return TEMPLATES.TemplateResponse(
                request, "not_found.html", {"title": "Work not found"}, status_code=404
            )
        if user.role != "admin" and entry.work.owner_user_id != user.id:
            return RedirectResponse(entry.public_url_path, status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "work_detail.html",
            {
                "title": entry.work.translated_title or entry.work.original_title,
                "current_user": user,
                "entry": entry,
            },
        )

    @app.get("/read/{token_slug}", response_class=HTMLResponse)
    def read_work(request: Request, token_slug: str):
        user = current_user(request)
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
                    "current_user": user,
                    "can_manage": bool(
                        user
                        and (
                            user.role == "admin" or user.id == entry.work.owner_user_id
                        )
                    ),
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
                "current_user": user,
                "can_manage": bool(
                    user
                    and (user.role == "admin" or user.id == entry.work.owner_user_id)
                ),
                "public": True,
            },
        )

    @app.get("/read/{token_slug}/{chapter_no}", response_class=HTMLResponse)
    def read_chapter(request: Request, token_slug: str, chapter_no: int):
        user = current_user(request)
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
                "current_user": user,
                "can_manage": bool(
                    user
                    and (user.role == "admin" or user.id == entry.work.owner_user_id)
                ),
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


def current_user(request: Request) -> UserRecord | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return request.app.state.user_store.get_user(int(user_id))


def is_admin(request: Request) -> bool:
    user = current_user(request)
    return bool(user and user.role == "admin")


def require_login(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    return None


def require_admin(request: Request):
    user = current_user(request)
    if not user or user.role != "admin":
        return RedirectResponse("/login", status_code=303)
    return None


def serve() -> None:
    settings = Settings()
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    serve()
