from __future__ import annotations

from pathlib import Path
import secrets
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
from fanfictl.pixiv_oauth import (
    create_oauth_session,
    exchange_code_for_token,
    extract_code,
    looks_like_intermediate_redirect,
)
from fanfictl.quota import QuotaTracker
from fanfictl.pixiv_tokens import PixivTokenStore


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
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key,
        https_only=should_use_secure_session_cookie(settings.app_base_url),
        same_site="lax",
        session_cookie="fableport_session",
    )
    app.mount(
        "/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static"
    )
    app.state.settings = settings
    app.state.user_store = UserStore(settings)
    app.state.key_store = APIKeyStore(settings, app.state.user_store)
    app.state.pixiv_token_store = PixivTokenStore(settings, app.state.user_store)
    app.state.jobs = JobManager(
        settings,
        user_store=app.state.user_store,
        key_store=app.state.key_store,
    )

    def template_response(
        request: Request,
        template_name: str,
        context: dict,
        *,
        status_code: int = 200,
    ):
        payload = dict(context)
        payload.setdefault("csrf_token", ensure_csrf_token(request))
        return TEMPLATES.TemplateResponse(
            request,
            template_name,
            payload,
            status_code=status_code,
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
        return template_response(
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
                "personal_pixiv_tokens": app.state.pixiv_token_store.list_personal_tokens(
                    user
                ),
                "global_pixiv_tokens": app.state.pixiv_token_store.list_global_tokens(),
                "pixiv_oauth_pending_scope": request.session.get("pixiv_oauth_scope"),
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
        return template_response(
            request,
            "login.html",
            {
                "error": error,
                "public": True,
                "title": "Sign in",
            },
        )

    @app.post("/login")
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(...),
    ):
        validate_csrf(request, csrf_token)
        user = app.state.user_store.authenticate(username, password)
        if user:
            request.session.clear()
            request.session["csrf_token"] = new_csrf_token()
            request.session["user_id"] = user.id
            request.session["username"] = user.username
            request.session["role"] = user.role
            return RedirectResponse("/dashboard", status_code=303)
        return template_response(
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
    def logout(request: Request, csrf_token: str = Form(...)):
        validate_csrf(request, csrf_token)
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
        csrf_token: str = Form(...),
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
        validate_csrf(request, csrf_token)
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
    def add_personal_key(
        request: Request,
        api_key: str = Form(...),
        csrf_token: str = Form(...),
    ):
        redirect = require_login(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
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
    def delete_personal_key(request: Request, key_id: str, csrf_token: str = Form(...)):
        redirect = require_login(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        app.state.key_store.remove_user_key(user, key_id)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/keys/global")
    def add_global_key(
        request: Request,
        api_key: str = Form(...),
        csrf_token: str = Form(...),
    ):
        redirect = require_admin(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            app.state.key_store.add_global_key(api_key)
        except ValueError as exc:
            return render_dashboard(
                request, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/keys/global/{key_id}/delete")
    def delete_global_key(request: Request, key_id: str, csrf_token: str = Form(...)):
        redirect = require_admin(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        app.state.key_store.remove_global_key(key_id)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/pixiv/personal")
    def add_personal_pixiv_token(
        request: Request,
        refresh_token: str = Form(...),
        csrf_token: str = Form(...),
    ):
        redirect = require_login(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        try:
            app.state.pixiv_token_store.add_user_token(user, refresh_token)
        except ValueError as exc:
            return render_dashboard(
                request, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    def _begin_pixiv_oauth(request: Request, scope: str) -> RedirectResponse:
        verifier, state, auth_url = create_oauth_session()
        request.session["pixiv_oauth_verifier"] = verifier
        request.session["pixiv_oauth_state"] = state
        request.session["pixiv_oauth_scope"] = scope
        return RedirectResponse(auth_url, status_code=303)

    @app.post("/pixiv/personal/oauth/start")
    def start_personal_pixiv_oauth(request: Request, csrf_token: str = Form(...)):
        redirect = require_login(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        return _begin_pixiv_oauth(request, "personal")

    @app.post("/pixiv/personal/{token_id}/delete")
    def delete_personal_pixiv_token(
        request: Request,
        token_id: str,
        csrf_token: str = Form(...),
    ):
        redirect = require_login(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        app.state.pixiv_token_store.remove_user_token(user, token_id)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/pixiv/global")
    def add_global_pixiv_token(
        request: Request,
        refresh_token: str = Form(...),
        csrf_token: str = Form(...),
    ):
        redirect = require_admin(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            app.state.pixiv_token_store.add_global_token(refresh_token)
        except ValueError as exc:
            return render_dashboard(
                request, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/pixiv/global/oauth/start")
    def start_global_pixiv_oauth(request: Request, csrf_token: str = Form(...)):
        redirect = require_admin(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        return _begin_pixiv_oauth(request, "global")

    @app.post("/pixiv/global/{token_id}/delete")
    def delete_global_pixiv_token(
        request: Request,
        token_id: str,
        csrf_token: str = Form(...),
    ):
        redirect = require_admin(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        app.state.pixiv_token_store.remove_global_token(token_id)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/pixiv/oauth/complete")
    def complete_pixiv_oauth(
        request: Request,
        callback_input: str = Form(...),
        csrf_token: str = Form(...),
    ):
        redirect = require_login(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        verifier = request.session.get("pixiv_oauth_verifier")
        scope = request.session.get("pixiv_oauth_scope")
        if not verifier or not scope:
            return render_dashboard(
                request,
                error="Start the Pixiv login flow first before completing it.",
                status_code=400,
                active_tab="settings",
            )
        code = extract_code(callback_input.strip())
        if not code:
            if looks_like_intermediate_redirect(callback_input):
                return render_dashboard(
                    request,
                    error="That Pixiv URL is still an intermediate redirect and does not contain the OAuth code yet. Copy the final callback URL with code=... if it appears in the address bar, or open the browser Network tab and copy the callback?...code=... request URL instead.",
                    status_code=400,
                    active_tab="settings",
                )
            return render_dashboard(
                request,
                error="Could not extract a Pixiv OAuth code from that input.",
                status_code=400,
                active_tab="settings",
            )
        try:
            payload = exchange_code_for_token(code=code, code_verifier=verifier)
            refresh_token = payload.get("refresh_token", "")
            if not refresh_token:
                raise RuntimeError("Pixiv OAuth did not return a refresh token")
            if scope == "global":
                if user.role != "admin":
                    raise RuntimeError("Only admins can store global Pixiv tokens")
                app.state.pixiv_token_store.add_global_token(refresh_token)
            else:
                app.state.pixiv_token_store.add_user_token(user, refresh_token)
        except Exception as exc:  # noqa: BLE001
            return render_dashboard(
                request,
                error=f"Pixiv OAuth exchange failed: {exc}",
                status_code=400,
                active_tab="settings",
            )
        finally:
            request.session.pop("pixiv_oauth_verifier", None)
            request.session.pop("pixiv_oauth_state", None)
            request.session.pop("pixiv_oauth_scope", None)

        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/users")
    def create_user(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        role: str = Form("user"),
        csrf_token: str = Form(...),
    ):
        redirect = require_admin(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
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
        csrf_token: str = Form(...),
    ):
        redirect = require_login(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
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
            return template_response(
                request, "not_found.html", {"title": "Job not found"}, status_code=404
            )
        if user.role != "admin" and job.owner_user_id != user.id:
            return RedirectResponse("/dashboard", status_code=303)
        work_entry = (
            get_work_by_root_name(settings.output_dir, job.work_root_name)
            if job.work_root_name
            else None
        )
        return template_response(
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
            return template_response(
                request, "not_found.html", {"title": "Work not found"}, status_code=404
            )
        if user.role != "admin" and entry.work.owner_user_id != user.id:
            return RedirectResponse(entry.public_url_path, status_code=303)
        return template_response(
            request,
            "work_detail.html",
            {
                "title": entry.work.translated_title or entry.work.original_title,
                "current_user": user,
                "entry": entry,
            },
        )

    @app.post("/works/{root_name}/retranslate")
    def retranslate_work(
        request: Request,
        root_name: str,
        csrf_token: str = Form(...),
    ):
        redirect = require_login(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        user = current_user(request)
        entry = get_work_by_root_name(settings.output_dir, root_name)
        if not entry or not user:
            return template_response(
                request, "not_found.html", {"title": "Work not found"}, status_code=404
            )
        if user.role != "admin" and entry.work.owner_user_id != user.id:
            return RedirectResponse("/dashboard", status_code=303)

        owner_user = (
            app.state.user_store.get_user(entry.work.owner_user_id)
            if entry.work.owner_user_id is not None
            else None
        )
        formats = [ExportFormat(fmt) for fmt in entry.outputs.keys()] or [
            ExportFormat.MD,
            ExportFormat.TXT,
            ExportFormat.HTML,
            ExportFormat.EPUB,
        ]
        job = app.state.jobs.start_job(
            entry.work.source_url,
            resume=False,
            chapter_limit=None,
            formats=formats,
            model=None,
            owner_user=owner_user,
        )
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/read/{token_slug}", response_class=HTMLResponse)
    def read_work(request: Request, token_slug: str):
        user = current_user(request)
        public_id = token_slug.split("-", 1)[0]
        entry = get_work_by_public_id(settings.output_dir, public_id)
        if not entry:
            return template_response(
                request,
                "not_found.html",
                {"title": "Work not found", "public": True},
                status_code=404,
            )
        if entry.work.kind.value == "series":
            return template_response(
                request,
                "reader_series.html",
                {
                    "title": entry.work.translated_title or entry.work.original_title,
                    "entry": entry,
                    "current_user": user,
                    "asset_base_href": f"/reader-assets/{entry.work.public_id}/",
                    "can_manage": bool(
                        user
                        and (
                            user.role == "admin" or user.id == entry.work.owner_user_id
                        )
                    ),
                    "public": True,
                },
            )
        return template_response(
            request,
            "reader.html",
            {
                "title": entry.work.translated_title or entry.work.original_title,
                "entry": entry,
                "body_html": render_work_html(entry.work),
                "current_user": user,
                "asset_base_href": f"/reader-assets/{entry.work.public_id}/",
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
            return template_response(
                request,
                "not_found.html",
                {"title": "Chapter not found", "public": True},
                status_code=404,
            )
        chapter = entry.work.chapters[chapter_no - 1]
        return template_response(
            request,
            "reader_chapter.html",
            {
                "title": chapter.translated_title or chapter.original_title,
                "entry": entry,
                "chapter": chapter,
                "chapter_no": chapter_no,
                "body_html": render_chapter_html(entry.work, chapter_no),
                "current_user": user,
                "asset_base_href": f"/reader-assets/{entry.work.public_id}/",
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
        return FileResponse(path, filename=filename)

    @app.get("/reader-assets/{public_id}/{asset_path:path}")
    def reader_asset(public_id: str, asset_path: str):
        entry = get_work_by_public_id(settings.output_dir, public_id)
        if not entry:
            return RedirectResponse("/", status_code=303)
        requested = (entry.root / asset_path).resolve()
        root = entry.root.resolve()
        if root != requested and root not in requested.parents:
            raise HTTPException(status_code=404, detail="Asset not found")
        if not requested.exists() or not requested.is_file():
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(requested)

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


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = new_csrf_token()
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, submitted_token: str) -> None:
    session_token = request.session.get("csrf_token")
    if not session_token or not submitted_token:
        raise HTTPException(status_code=403, detail="Missing CSRF token")
    if not secrets.compare_digest(session_token, submitted_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    validate_same_origin(request)


def validate_same_origin(request: Request) -> None:
    settings = getattr(request.app.state, "settings", None)
    allowed = urlparse(
        settings.app_base_url
        if settings and getattr(settings, "app_base_url", None)
        else str(request.base_url)
    )
    allowed_origin = f"{allowed.scheme}://{allowed.netloc}"
    for header_name in ("origin", "referer"):
        value = request.headers.get(header_name)
        if not value:
            continue
        parsed = urlparse(value)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin != allowed_origin:
            raise HTTPException(
                status_code=403, detail="Cross-site form submission blocked"
            )


def should_use_secure_session_cookie(app_base_url: str) -> bool:
    parsed = urlparse(app_base_url)
    return parsed.scheme == "https" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
    }


def serve() -> None:
    settings = Settings()
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    serve()
