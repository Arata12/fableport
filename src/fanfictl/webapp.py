from __future__ import annotations

from contextlib import asynccontextmanager
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
from fanfictl.colab import (
    VALID_ENGINES,
    VALID_GPUS,
    ColabManager,
    create_colab_oauth_session,
    exchange_colab_code,
    extract_colab_code,
    normalize_colab_model_name,
)
from fanfictl.config import Settings, VALID_BACKENDS, is_ollama_cloud_host
from fanfictl.content import normalize_target_language_name
from fanfictl.jobs import JobManager
from fanfictl.keystore import APIKeyStore
from fanfictl.library import (
    delete_work_by_root_name,
    get_work_by_public_id,
    get_work_by_root_name,
    list_works,
    output_filename,
    render_chapter_html,
    render_work_html,
)
from fanfictl.models import ExportFormat, WorkKind
from fanfictl.pixiv_oauth import (
    create_oauth_session,
    exchange_code_for_token,
    extract_code,
    looks_like_intermediate_redirect,
)
from fanfictl.pixiv_tokens import PixivTokenStore
from fanfictl.quota import QuotaTracker
from fanfictl.translate import test_llm_connection
from fanfictl.workflow import parse_pixiv_urls


class LoginRequired(Exception):
    pass


PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))


def _set_flash(
    request: Request, *, notice: str | None = None, error: str | None = None
) -> None:
    if notice:
        request.session["flash_notice"] = notice
    if error:
        request.session["flash_error"] = error


def _pop_flash(request: Request) -> tuple[str | None, str | None]:
    return (
        request.session.pop("flash_notice", None),
        request.session.pop("flash_error", None),
    )


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if not settings.app_secret_key:
        settings.app_secret_key = load_or_create_secret(settings.output_dir)

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        yield
        app_instance.state.jobs.shutdown()
        app_instance.state.colab.shutdown()

    app = FastAPI(title="Fableport", lifespan=lifespan)
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
    app.state.colab = ColabManager(settings)
    app.state.jobs = JobManager(
        settings,
        user_store=app.state.user_store,
        key_store=app.state.key_store,
        pixiv_token_store=app.state.pixiv_token_store,
        colab_manager=app.state.colab,
    )

    @app.exception_handler(LoginRequired)
    def login_required_handler(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    def template_response(
        request: Request,
        template_name: str,
        context: dict,
        *,
        status_code: int = 200,
    ):
        payload = dict(context)
        payload.setdefault("csrf_token", ensure_csrf_token(request))
        payload.setdefault("login_enabled", settings.require_login)
        return TEMPLATES.TemplateResponse(
            request,
            template_name,
            payload,
            status_code=status_code,
        )

    def check_translation_ready(user: UserRecord) -> tuple[int, str] | None:
        if settings.colab_enabled:
            colab_status = app.state.colab.status()
            if not colab_status.cli_available:
                return (
                    400,
                    "Managed Google Colab is enabled, but google-colab-cli / ssh is not installed in this runtime.",
                )
            if not colab_status.authenticated:
                return (
                    400,
                    "Connect your Google Colab account in Settings before translating with Colab.",
                )
            if not settings.colab_auto_start and colab_status.state != "ready":
                return (
                    400,
                    "Colab GPU is stopped and auto-start is disabled. Start Colab first or enable auto-start in Settings.",
                )
            return None

        runtime_keys = app.state.key_store.runtime_keys_for_user(user)
        if settings.requires_api_key and not runtime_keys:
            return (
                400,
                "Add an Ollama API key in Settings before translating with Ollama Cloud (or switch to a local / OpenAI-compatible host).",
            )
        if runtime_keys and QuotaTracker(settings, runtime_keys).daily_limit_reached():
            return (
                429,
                "Daily request limit reached. Wait until the reset time shown below.",
            )
        return None

    def render_dashboard(
        request: Request,
        user: UserRecord,
        error: str | None = None,
        notice: str | None = None,
        status_code: int = 200,
        active_tab: str = "overview",
    ):
        flash_notice, flash_error = _pop_flash(request)
        effective_error = error or flash_error
        effective_notice = notice or flash_notice
        runtime_keys = app.state.key_store.runtime_keys_for_user(user)
        quota = QuotaTracker(settings, runtime_keys).snapshot()
        works = list_works(settings.output_dir)
        my_works = [
            entry
            for entry in works
            if entry.work.owner_user_id == user.id
            or (user.role == "admin" and entry.work.owner_user_id is None)
        ]
        public_works = [entry for entry in works if entry not in my_works]
        pending_my_works = [
            entry for entry in my_works if not entry.work.is_translated
        ]
        pending_all_works = [
            entry for entry in works if not entry.work.is_translated
        ]
        if user.role == "admin":
            jobs = app.state.jobs.store.list_recent()
        else:
            own_jobs = [
                job
                for job in app.state.jobs.store.list_all()
                if job.owner_user_id == user.id
            ]
            jobs = sorted(own_jobs, key=lambda job: job.updated_at, reverse=True)[:12]
        return template_response(
            request,
            "dashboard.html",
            {
                "title": "Dashboard",
                "current_user": user,
                "jobs": jobs,
                "my_works": my_works,
                "public_works": public_works,
                "pending_my_count": len(pending_my_works),
                "pending_all_count": len(pending_all_works),
                "using_default_admin": settings.require_login
                and settings.uses_default_admin_credentials,
                "base_url": settings.app_base_url,
                "quota": quota,
                "personal_keys": app.state.key_store.list_personal_keys(user),
                "global_keys": app.state.key_store.list_global_keys(),
                "personal_pixiv_tokens": app.state.pixiv_token_store.list_personal_tokens(
                    user
                ),
                "global_pixiv_tokens": app.state.pixiv_token_store.list_global_tokens(),
                "pixiv_oauth_pending_scope": request.session.get("pixiv_oauth_scope"),
                "colab_oauth_pending": bool(request.session.get("colab_oauth_verifier")),
                "colab_status": app.state.colab.status(),
                "colab_gpus": VALID_GPUS,
                "colab_engines": VALID_ENGINES,
                "users": app.state.user_store.list_users()
                if user.role == "admin"
                else [],
                "app_config": settings,
                "form_error": effective_error,
                "form_notice": effective_notice,
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
        if not settings.require_login:
            return RedirectResponse("/dashboard", status_code=303)
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
        return render_dashboard(request, require_user(request))

    @app.get("/dashboard/library", response_class=HTMLResponse)
    def dashboard_library(request: Request):
        return render_dashboard(request, require_user(request), active_tab="library")

    @app.get("/dashboard/settings", response_class=HTMLResponse)
    def dashboard_settings(request: Request):
        return render_dashboard(request, require_user(request), active_tab="settings")

    @app.post("/submit")
    def submit(
        request: Request,
        csrf_token: str = Form(...),
        source_urls: str | None = Form(None),
        source_url: str | None = Form(None),
        action: str = Form("import"),
        resume: str | None = Form(None),
        chapter_limit: int | None = Form(None),
        export_md: str | None = Form(None),
        export_txt: str | None = Form(None),
        export_html: str | None = Form(None),
        export_epub: str | None = Form(None),
    ):
        user = require_user(request)
        validate_csrf(request, csrf_token)
        raw_input = (source_urls if source_urls is not None else source_url) or ""
        try:
            parsed_urls = parse_pixiv_urls(raw_input)
        except ValueError:
            return render_dashboard(
                request, user, error="Unsupported Pixiv URL", status_code=400
            )

        if action == "import":
            jobs = [
                app.state.jobs.start_import_job(
                    item.url,
                    chapter_limit=chapter_limit,
                    owner_user=user,
                )
                for item in parsed_urls
            ]
            if len(jobs) == 1:
                return RedirectResponse(f"/jobs/{jobs[0].id}", status_code=303)
            _set_flash(
                request,
                notice=f"Queued {len(jobs)} import job(s). Once finished, open Library to translate them.",
            )
            return RedirectResponse("/dashboard", status_code=303)

        blocked = check_translation_ready(user)
        if blocked:
            code, message = blocked
            return render_dashboard(request, user, error=message, status_code=code)

        selected = {
            ExportFormat.MD: export_md,
            ExportFormat.TXT: export_txt,
            ExportFormat.HTML: export_html,
            ExportFormat.EPUB: export_epub,
        }
        formats = [fmt for fmt, checked in selected.items() if checked] or list(
            ExportFormat
        )
        jobs = [
            app.state.jobs.start_job(
                item.url,
                resume=bool(resume),
                chapter_limit=chapter_limit,
                formats=formats,
                model=None,
                owner_user=user,
            )
            for item in parsed_urls
        ]
        if len(jobs) == 1:
            return RedirectResponse(f"/jobs/{jobs[0].id}", status_code=303)
        _set_flash(
            request,
            notice=f"Queued {len(jobs)} import & translation job(s).",
        )
        return RedirectResponse("/dashboard", status_code=303)

    @app.post("/keys/personal")
    def add_personal_key(
        request: Request,
        api_key: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = require_user(request)
        validate_csrf(request, csrf_token)
        try:
            app.state.key_store.add_user_key(user, api_key)
        except ValueError as exc:
            return render_dashboard(
                request, user, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/keys/personal/{key_id}/delete")
    def delete_personal_key(request: Request, key_id: str, csrf_token: str = Form(...)):
        user = require_user(request)
        validate_csrf(request, csrf_token)
        app.state.key_store.remove_user_key(user, key_id)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/keys/global")
    def add_global_key(
        request: Request,
        api_key: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        try:
            app.state.key_store.add_global_key(api_key)
        except ValueError as exc:
            return render_dashboard(
                request, user, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/keys/global/{key_id}/delete")
    def delete_global_key(request: Request, key_id: str, csrf_token: str = Form(...)):
        require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        app.state.key_store.remove_global_key(key_id)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/pixiv/personal")
    def add_personal_pixiv_token(
        request: Request,
        refresh_token: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = require_user(request)
        validate_csrf(request, csrf_token)
        try:
            app.state.pixiv_token_store.add_user_token(user, refresh_token)
        except ValueError as exc:
            return render_dashboard(
                request, user, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    def _begin_pixiv_oauth(request: Request, scope: str) -> RedirectResponse:
        verifier, _state, auth_url = create_oauth_session()
        request.session["pixiv_oauth_verifier"] = verifier
        request.session["pixiv_oauth_scope"] = scope
        return RedirectResponse(auth_url, status_code=303)

    @app.post("/pixiv/personal/oauth/start")
    def start_personal_pixiv_oauth(request: Request, csrf_token: str = Form(...)):
        require_user(request)
        validate_csrf(request, csrf_token)
        return _begin_pixiv_oauth(request, "personal")

    @app.post("/pixiv/personal/{token_id}/delete")
    def delete_personal_pixiv_token(
        request: Request,
        token_id: str,
        csrf_token: str = Form(...),
    ):
        user = require_user(request)
        validate_csrf(request, csrf_token)
        app.state.pixiv_token_store.remove_user_token(user, token_id)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/pixiv/global")
    def add_global_pixiv_token(
        request: Request,
        refresh_token: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        try:
            app.state.pixiv_token_store.add_global_token(refresh_token)
        except ValueError as exc:
            return render_dashboard(
                request, user, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/pixiv/global/oauth/start")
    def start_global_pixiv_oauth(request: Request, csrf_token: str = Form(...)):
        require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        return _begin_pixiv_oauth(request, "global")

    @app.post("/pixiv/global/{token_id}/delete")
    def delete_global_pixiv_token(
        request: Request,
        token_id: str,
        csrf_token: str = Form(...),
    ):
        require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        app.state.pixiv_token_store.remove_global_token(token_id)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/pixiv/oauth/complete")
    def complete_pixiv_oauth(
        request: Request,
        callback_input: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = require_user(request)
        validate_csrf(request, csrf_token)
        verifier = request.session.get("pixiv_oauth_verifier")
        scope = request.session.get("pixiv_oauth_scope")
        if not verifier or not scope:
            return render_dashboard(
                request,
                user,
                error="Start the Pixiv login flow first before completing it.",
                status_code=400,
                active_tab="settings",
            )
        code = extract_code(callback_input.strip())
        if not code:
            if looks_like_intermediate_redirect(callback_input):
                return render_dashboard(
                    request,
                    user,
                    error="That Pixiv URL is still an intermediate redirect and does not contain the OAuth code yet. Copy the final callback URL with code=... if it appears in the address bar, or open the browser Network tab and copy the callback?...code=... request URL instead.",
                    status_code=400,
                    active_tab="settings",
                )
            return render_dashboard(
                request,
                user,
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
                user,
                error=f"Pixiv OAuth exchange failed: {exc}",
                status_code=400,
                active_tab="settings",
            )
        finally:
            request.session.pop("pixiv_oauth_verifier", None)
            request.session.pop("pixiv_oauth_scope", None)

        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/settings/translation")
    def save_translation_settings(
        request: Request,
        csrf_token: str = Form(...),
        provider_backend: str | None = Form(None),
        ollama_model: str | None = Form(None),
        cloud_model: str | None = Form(None),
        ollama_host: str | None = Form(None),
        translation_concurrency: int = Form(3),
        context_length: int | None = Form(None),
        rpm_limit: int = Form(0),
        rpd_limit: int = Form(0),
        api_mode: str | None = Form(None),
        target_language: str | None = Form(None),
        colab_gpu: str | None = Form(None),
        colab_high_mem: str | None = Form(None),
        colab_engine: str | None = Form(None),
        colab_model: str | None = Form(None),
        colab_extra_args: str | None = Form(None),
        colab_hf_token: str | None = Form(None),
        clear_hf_token: str | None = Form(None),
        colab_auto_start: str | None = Form(None),
        colab_auto_stop: str | None = Form(None),
        colab_idle_stop_minutes: int | None = Form(None),
        colab_session_name: str | None = Form(None),
        colab_form_present: str | None = Form(None),
    ):
        user = require_user(request, admin=True)
        validate_csrf(request, csrf_token)

        lang = normalize_target_language_name(
            target_language if target_language is not None else settings.target_language
        )
        ctx_len = settings.context_length if context_length is None else context_length
        host_raw = (
            ollama_host if ollama_host is not None else settings.ollama_host
        ).strip().rstrip("/")
        if provider_backend is not None:
            backend = provider_backend.strip().lower()
        else:
            backend = "ollama_cloud" if is_ollama_cloud_host(host_raw) else "local"

        if backend == "ollama_cloud":
            model = (cloud_model or ollama_model or settings.ollama_model).strip()
            host = "https://ollama.com"
            mode = "ollama"
        elif backend == "local":
            model = (ollama_model if ollama_model is not None else settings.ollama_model).strip()
            host = host_raw
            mode = (api_mode or settings.api_mode).strip().lower()
        else:
            model = (ollama_model or settings.ollama_model).strip()
            host = host_raw or settings.ollama_host
            mode = (api_mode or settings.api_mode).strip().lower()

        error = None
        if backend not in VALID_BACKENDS:
            error = "Invalid translation provider."
        elif not lang:
            error = "Target language cannot be empty."
        elif not 1 <= translation_concurrency <= 16:
            error = "Parallel chapters must be between 1 and 16."
        elif ctx_len != 0 and not 4096 <= ctx_len <= 262144:
            error = "Context window must be Auto (0) or between 4096 and 262144."
        elif rpm_limit < 0 or rpd_limit < 0:
            error = "Limits cannot be negative (use 0 for no limit)."
        elif backend == "local":
            if mode not in {"ollama", "openai"}:
                error = "API mode must be 'ollama' or 'openai'."
            elif not model:
                error = "Model cannot be empty."
            elif not host.startswith(("http://", "https://")):
                error = "Host must start with http:// or https://."
        elif backend == "ollama_cloud" and not model:
            error = "Model cannot be empty."

        updates: dict[str, str] = {
            "provider_backend": backend,
            "colab_enabled": "1" if backend == "colab" else "0",
            "api_mode": mode,
            "target_language": lang,
            "context_length": str(ctx_len),
            "ollama_model": model or settings.ollama_model,
            "ollama_host": host or settings.ollama_host,
            "translation_concurrency": str(translation_concurrency),
            "rpm_limit": str(rpm_limit),
            "rpd_limit": str(rpd_limit),
        }

        if colab_form_present or backend == "colab":
            gpu = (colab_gpu or settings.colab_gpu).strip().upper()
            engine = (colab_engine or settings.colab_engine).strip().lower()
            session_name = (colab_session_name or settings.colab_session_name).strip()
            idle_min = (
                settings.colab_idle_stop_minutes
                if colab_idle_stop_minutes is None
                else colab_idle_stop_minutes
            )
            if not error:
                if gpu not in VALID_GPUS:
                    error = f"Unsupported Colab GPU '{gpu}'."
                elif engine not in VALID_ENGINES:
                    error = "Colab engine must be 'vllm' or 'ollama'."
                elif not session_name or any(ch.isspace() for ch in session_name):
                    error = "Session name must be non-empty without spaces."
                elif not 0 <= idle_min <= 120:
                    error = "Idle auto-stop minutes must be between 0 and 120."
            updates.update(
                {
                    "colab_gpu": gpu,
                    "colab_engine": engine,
                    "colab_model": normalize_colab_model_name(
                        (
                            colab_model
                            if colab_model is not None
                            else settings.colab_model
                        ).strip(),
                        engine,
                    ),
                    "colab_extra_args": (
                        colab_extra_args
                        if colab_extra_args is not None
                        else settings.colab_extra_args
                    ).strip(),
                    "colab_session_name": session_name,
                    "colab_idle_stop_minutes": str(idle_min),
                }
            )
            if colab_form_present:
                updates["colab_high_mem"] = "1" if colab_high_mem else "0"
                updates["colab_auto_start"] = "1" if colab_auto_start else "0"
                updates["colab_auto_stop"] = "1" if colab_auto_stop else "0"
            if clear_hf_token:
                updates["colab_hf_token"] = ""
            elif colab_hf_token and colab_hf_token.strip():
                updates["colab_hf_token"] = colab_hf_token.strip()

        if error:
            return render_dashboard(
                request, user, error=error, status_code=400, active_tab="settings"
            )
        app.state.user_store.save_app_settings(updates)
        app.state.jobs.notify_concurrency_changed()
        _set_flash(request, notice="Translation settings saved.")
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/settings/translation/test")
    def test_translation_connection(
        request: Request,
        csrf_token: str = Form(...),
        provider_backend: str | None = Form(None),
        ollama_host: str | None = Form(None),
        api_mode: str | None = Form(None),
        ollama_model: str | None = Form(None),
        cloud_model: str | None = Form(None),
    ):
        user = require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        backend = (provider_backend or settings.provider_backend).strip().lower()
        if backend == "colab":
            colab_status = app.state.colab.status()
            if colab_status.state != "ready":
                _set_flash(
                    request,
                    error=f"Google Colab GPU is currently '{colab_status.state}'. Start Colab GPU first to test its endpoint.",
                )
                return RedirectResponse("/dashboard/settings", status_code=303)
            result = test_llm_connection(
                host=colab_status.local_url,
                api_mode="openai" if colab_status.engine == "vllm" else "ollama",
                model_name=colab_status.model,
                api_key=None,
            )
        elif backend == "ollama_cloud":
            runtime_keys = app.state.key_store.runtime_keys_for_user(user)
            api_key = runtime_keys[0].key if runtime_keys else None
            result = test_llm_connection(
                host="https://ollama.com",
                api_mode="ollama",
                model_name=(cloud_model or ollama_model or settings.ollama_model).strip(),
                api_key=api_key,
            )
        else:
            runtime_keys = app.state.key_store.runtime_keys_for_user(user)
            api_key = runtime_keys[0].key if runtime_keys else None
            host_to_test = (ollama_host or settings.ollama_host).strip().rstrip("/")
            mode_to_test = (api_mode or settings.api_mode).strip().lower()
            model_to_test = (ollama_model or settings.ollama_model).strip()
            result = test_llm_connection(
                host=host_to_test,
                api_mode=mode_to_test,
                model_name=model_to_test,
                api_key=api_key,
            )
        if result.ok:
            _set_flash(request, notice=result.message)
        else:
            _set_flash(request, error=result.message)
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/settings/colab")
    def save_colab_settings(
        request: Request,
        csrf_token: str = Form(...),
        colab_enabled: str | None = Form(None),
        colab_gpu: str = Form("L4"),
        colab_high_mem: str | None = Form(None),
        colab_engine: str = Form("vllm"),
        colab_model: str = Form(""),
        colab_extra_args: str = Form(""),
        colab_hf_token: str = Form(""),
        clear_hf_token: str | None = Form(None),
        colab_auto_start: str | None = Form(None),
        colab_auto_stop: str | None = Form(None),
        colab_idle_stop_minutes: int = Form(0),
        colab_session_name: str = Form("fableport"),
    ):
        user = require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        gpu = colab_gpu.strip().upper()
        engine = colab_engine.strip().lower()
        session_name = colab_session_name.strip()
        error = None
        if gpu not in VALID_GPUS:
            error = f"Unsupported Colab GPU '{gpu}'. Choose one of: {', '.join(VALID_GPUS)}."
        elif engine not in VALID_ENGINES:
            error = "Colab engine must be 'vllm' or 'ollama'."
        elif not session_name or any(ch.isspace() for ch in session_name):
            error = "Session name must be non-empty without spaces."
        elif not 0 <= colab_idle_stop_minutes <= 120:
            error = "Idle auto-stop minutes must be between 0 and 120."
        if error:
            return render_dashboard(
                request, user, error=error, status_code=400, active_tab="settings"
            )

        updates = {
            "provider_backend": "colab"
            if colab_enabled
            else ("ollama_cloud" if is_ollama_cloud_host(settings.ollama_host) else "local"),
            "colab_enabled": "1" if colab_enabled else "0",
            "colab_gpu": gpu,
            "colab_high_mem": "1" if colab_high_mem else "0",
            "colab_engine": engine,
            "colab_model": normalize_colab_model_name(colab_model.strip(), engine),
            "colab_extra_args": colab_extra_args.strip(),
            "colab_auto_start": "1" if colab_auto_start else "0",
            "colab_auto_stop": "1" if colab_auto_stop else "0",
            "colab_idle_stop_minutes": str(colab_idle_stop_minutes),
            "colab_session_name": session_name,
        }
        if clear_hf_token:
            updates["colab_hf_token"] = ""
        elif colab_hf_token.strip():
            updates["colab_hf_token"] = colab_hf_token.strip()

        app.state.user_store.save_app_settings(updates)
        _set_flash(request, notice="Google Colab runtime settings saved.")
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/colab/oauth/start")
    def start_colab_oauth(request: Request, csrf_token: str = Form(...)):
        require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        verifier, _state, auth_url = create_colab_oauth_session()
        request.session["colab_oauth_verifier"] = verifier
        return RedirectResponse(auth_url, status_code=303)

    @app.post("/colab/oauth/complete")
    def complete_colab_oauth(
        request: Request,
        auth_code: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        verifier = request.session.get("colab_oauth_verifier")
        if not verifier:
            return render_dashboard(
                request,
                user,
                error="Start the Google Colab login flow first before completing it.",
                status_code=400,
                active_tab="settings",
            )
        code = extract_colab_code(auth_code)
        if not code:
            return render_dashboard(
                request,
                user,
                error="Could not extract an authorization code from that input.",
                status_code=400,
                active_tab="settings",
            )
        try:
            adc_payload = exchange_colab_code(code=code, code_verifier=verifier)
            app.state.colab.save_adc_credentials(adc_payload)
        except Exception as exc:  # noqa: BLE001
            return render_dashboard(
                request,
                user,
                error=f"Google Colab login failed: {exc}",
                status_code=400,
                active_tab="settings",
            )
        finally:
            request.session.pop("colab_oauth_verifier", None)

        _set_flash(request, notice="Google Colab account connected.")
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/colab/adc")
    def save_colab_adc(
        request: Request,
        adc_json: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        try:
            app.state.colab.save_adc_credentials(adc_json)
        except ValueError as exc:
            return render_dashboard(
                request, user, error=str(exc), status_code=400, active_tab="settings"
            )
        _set_flash(request, notice="Google Colab ADC credentials saved.")
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.post("/colab/disconnect")
    def disconnect_colab(request: Request, csrf_token: str = Form(...)):
        require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        app.state.jobs.cancel_queued_translations(
            "Google Colab was disconnected by user"
        )
        app.state.colab.stop()
        app.state.colab.clear_adc_credentials()
        request.session.pop("colab_oauth_verifier", None)
        _set_flash(request, notice="Google Colab credentials removed.")
        return RedirectResponse("/dashboard/settings", status_code=303)

    def _safe_return_path(return_to: str | None) -> str:
        if return_to in {"/dashboard", "/dashboard/library", "/dashboard/settings"}:
            return return_to
        return "/dashboard/settings"

    @app.post("/colab/start")
    def start_colab(
        request: Request,
        csrf_token: str = Form(...),
        return_to: str | None = Form(None),
    ):
        require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        colab_status = app.state.colab.status()
        if not colab_status.cli_available:
            _set_flash(
                request,
                error="google-colab-cli / ssh is not installed in this container.",
            )
        elif not colab_status.authenticated:
            _set_flash(
                request,
                error="Connect your Google Colab account in Settings first.",
            )
        else:
            app.state.colab.start_async()
            display_model = normalize_colab_model_name(
                settings.effective_colab_model, settings.colab_engine
            )
            _set_flash(
                request,
                notice=f"Starting Colab {settings.colab_gpu} session ({settings.colab_engine} · {display_model})...",
            )
        return RedirectResponse(_safe_return_path(return_to), status_code=303)

    @app.post("/colab/stop")
    def stop_colab(
        request: Request,
        csrf_token: str = Form(...),
        return_to: str | None = Form(None),
    ):
        require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        cancelled = app.state.jobs.cancel_queued_translations(
            "Colab GPU was stopped by user"
        )
        app.state.colab.stop()
        msg = (
            f"Google Colab session stopped (cancelled {cancelled} queued job(s))."
            if cancelled
            else "Google Colab session stopped."
        )
        _set_flash(request, notice=msg)
        return RedirectResponse(_safe_return_path(return_to), status_code=303)

    @app.post("/colab/usage")
    def refresh_colab_usage(
        request: Request,
        csrf_token: str = Form(...),
        return_to: str | None = Form(None),
    ):
        require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        info = app.state.colab.refresh_usage()
        if info.error:
            _set_flash(request, error=f"Could not fetch Colab balance: {info.error}")
        elif info.balance:
            _set_flash(request, notice=f"Colab balance: {info.balance}")
        return RedirectResponse(_safe_return_path(return_to), status_code=303)

    @app.post("/users")
    def create_user(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        role: str = Form("user"),
        csrf_token: str = Form(...),
    ):
        user = require_user(request, admin=True)
        validate_csrf(request, csrf_token)
        try:
            app.state.user_store.create_user(
                username=username, password=password, role=role
            )
        except ValueError as exc:
            return render_dashboard(
                request, user, error=str(exc), status_code=400, active_tab="settings"
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
        user = require_user(request)
        validate_csrf(request, csrf_token)
        if new_password != confirm_password:
            return render_dashboard(
                request,
                user,
                error="New password and confirmation do not match.",
                status_code=400,
                active_tab="settings",
            )
        try:
            app.state.user_store.change_password(user, current_password, new_password)
        except ValueError as exc:
            return render_dashboard(
                request, user, error=str(exc), status_code=400, active_tab="settings"
            )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        user = require_user(request)
        job = app.state.jobs.store.get(job_id)
        if not job:
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
        user = require_user(request)
        entry = get_work_by_root_name(settings.output_dir, root_name)
        if not entry:
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
                "app_config": settings,
            },
        )

    @app.post("/works/translate-all")
    def translate_all_pending(
        request: Request,
        csrf_token: str = Form(...),
        scope: str = Form("mine"),
    ):
        user = require_user(request)
        validate_csrf(request, csrf_token)
        blocked = check_translation_ready(user)
        if blocked:
            code, message = blocked
            return render_dashboard(
                request, user, error=message, status_code=code, active_tab="library"
            )

        works = list_works(settings.output_dir)
        if scope == "all" and user.role == "admin":
            candidates = works
        else:
            candidates = [
                entry
                for entry in works
                if entry.work.owner_user_id == user.id
                or (user.role == "admin" and entry.work.owner_user_id is None)
            ]
        pending = [entry for entry in candidates if not entry.work.is_translated]
        if not pending:
            _set_flash(request, notice="All works are already translated.")
            return RedirectResponse("/dashboard/library", status_code=303)

        queued_ids: list[str] = []
        for entry in pending:
            owner_user = (
                app.state.user_store.get_user(entry.work.owner_user_id)
                if entry.work.owner_user_id is not None
                else user
            )
            formats = [ExportFormat(fmt) for fmt in entry.outputs] or list(ExportFormat)
            job = app.state.jobs.start_translate_job(
                entry.root_name,
                resume=True,
                formats=formats,
                model=None,
                target_language=settings.target_language,
                refresh_source=False,
                owner_user=owner_user,
            )
            queued_ids.append(job.id)

        _set_flash(
            request,
            notice=f"Queued {len(queued_ids)} pending work(s) for translation into {settings.target_language}.",
        )
        return RedirectResponse("/dashboard", status_code=303)

    @app.post("/works/{root_name}/translate")
    def translate_work_route(
        request: Request,
        root_name: str,
        csrf_token: str = Form(...),
        resume: str | None = Form("1"),
        refresh_source: str | None = Form(None),
    ):
        user = require_user(request)
        validate_csrf(request, csrf_token)
        entry = get_work_by_root_name(settings.output_dir, root_name)
        if not entry:
            return template_response(
                request, "not_found.html", {"title": "Work not found"}, status_code=404
            )
        if user.role != "admin" and entry.work.owner_user_id != user.id:
            return RedirectResponse("/dashboard", status_code=303)

        blocked = check_translation_ready(user)
        if blocked:
            code, message = blocked
            return render_dashboard(
                request, user, error=message, status_code=code, active_tab="library"
            )

        owner_user = (
            app.state.user_store.get_user(entry.work.owner_user_id)
            if entry.work.owner_user_id is not None
            else user
        )
        formats = [ExportFormat(fmt) for fmt in entry.outputs] or list(ExportFormat)
        job = app.state.jobs.start_translate_job(
            entry.root_name,
            resume=bool(resume),
            formats=formats,
            model=None,
            target_language=settings.target_language,
            refresh_source=bool(refresh_source),
            owner_user=owner_user,
        )
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/works/{root_name}/retranslate")
    def retranslate_work(
        request: Request,
        root_name: str,
        csrf_token: str = Form(...),
        refresh_source: str | None = Form(None),
    ):
        user = require_user(request)
        validate_csrf(request, csrf_token)
        entry = get_work_by_root_name(settings.output_dir, root_name)
        if not entry:
            return template_response(
                request, "not_found.html", {"title": "Work not found"}, status_code=404
            )
        if user.role != "admin" and entry.work.owner_user_id != user.id:
            return RedirectResponse("/dashboard", status_code=303)

        blocked = check_translation_ready(user)
        if blocked:
            code, message = blocked
            return render_dashboard(
                request, user, error=message, status_code=code, active_tab="library"
            )

        owner_user = (
            app.state.user_store.get_user(entry.work.owner_user_id)
            if entry.work.owner_user_id is not None
            else user
        )
        formats = [ExportFormat(fmt) for fmt in entry.outputs] or list(ExportFormat)
        job = app.state.jobs.start_translate_job(
            entry.root_name,
            resume=False,
            formats=formats,
            model=None,
            target_language=settings.target_language,
            refresh_source=bool(refresh_source),
            owner_user=owner_user,
        )
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/works/{root_name}/delete")
    def delete_work_route(
        request: Request,
        root_name: str,
        csrf_token: str = Form(...),
    ):
        user = require_user(request)
        validate_csrf(request, csrf_token)
        entry = get_work_by_root_name(settings.output_dir, root_name)
        if not entry:
            return template_response(
                request, "not_found.html", {"title": "Work not found"}, status_code=404
            )
        if user.role != "admin" and entry.work.owner_user_id != user.id:
            return RedirectResponse("/dashboard/library", status_code=303)
        if app.state.jobs.has_active_job_for_work(root_name):
            _set_flash(
                request,
                error="Wait for the active job on this work to finish before deleting it.",
            )
            return RedirectResponse("/dashboard/library", status_code=303)

        title = entry.work.translated_title or entry.work.original_title
        delete_work_by_root_name(settings.output_dir, root_name)
        app.state.jobs.delete_finished_jobs_for_work(root_name)
        _set_flash(request, notice=f"Deleted '{title}'.")
        return RedirectResponse("/dashboard/library", status_code=303)

    def reader_context(request: Request, entry) -> dict:
        user = current_user(request)
        return {
            "entry": entry,
            "current_user": user,
            "asset_base_href": f"/reader-assets/{entry.work.public_id}/",
            "can_manage": bool(
                user and (user.role == "admin" or user.id == entry.work.owner_user_id)
            ),
            "public": True,
        }

    @app.get("/read/{token_slug}", response_class=HTMLResponse)
    def read_work(request: Request, token_slug: str):
        public_id = token_slug.split("-", 1)[0]
        entry = get_work_by_public_id(settings.output_dir, public_id)
        if not entry:
            return template_response(
                request,
                "not_found.html",
                {"title": "Work not found", "public": True},
                status_code=404,
            )
        context = reader_context(request, entry)
        context["title"] = entry.work.translated_title or entry.work.original_title
        if entry.work.kind == WorkKind.SERIES:
            return template_response(request, "reader_series.html", context)
        context["body_html"] = render_work_html(entry.work)
        return template_response(request, "reader.html", context)

    @app.get("/read/{token_slug}/{chapter_no}", response_class=HTMLResponse)
    def read_chapter(request: Request, token_slug: str, chapter_no: int):
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
        context = reader_context(request, entry)
        context.update(
            title=chapter.translated_title or chapter.original_title,
            chapter=chapter,
            chapter_no=chapter_no,
            body_html=render_chapter_html(entry.work, chapter_no),
        )
        return template_response(request, "reader_chapter.html", context)

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
        assets_root = (entry.root / "assets").resolve()
        requested = (entry.root / asset_path).resolve()
        if not requested.is_relative_to(assets_root) or not requested.is_file():
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(requested)

    return app


def current_user(request: Request) -> UserRecord | None:
    if not request.app.state.settings.require_login:
        return request.app.state.user_store.get_admin()
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return request.app.state.user_store.get_user(int(user_id))


def require_user(request: Request, *, admin: bool = False) -> UserRecord:
    user = current_user(request)
    if not user or (admin and user.role != "admin"):
        raise LoginRequired()
    return user


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
    """Reject form posts whose Origin/Referer is another site.

    Both the configured base URL and the host the request came in on are
    accepted, so the app also works via localhost or a Tailscale address.
    """
    allowed = {
        urlparse(request.app.state.settings.app_base_url).netloc,
        urlparse(str(request.base_url)).netloc,
    }
    for header_name in ("origin", "referer"):
        value = request.headers.get(header_name)
        if value and urlparse(value).netloc not in allowed:
            raise HTTPException(
                status_code=403, detail="Cross-site form submission blocked"
            )


def load_or_create_secret(output_dir: Path) -> str:
    """Session signing key, generated on first start and kept with the data."""
    path = output_dir / ".secret_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    output_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    path.write_text(secret, encoding="utf-8")
    path.chmod(0o600)
    return secret


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
