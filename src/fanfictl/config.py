from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


load_dotenv()


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# Settings that are allowed to be cleared back to an empty string via the web UI.
_ALLOW_EMPTY_OVERRIDES = {"colab_model", "colab_extra_args", "colab_hf_token"}

VALID_BACKENDS = ("ollama_cloud", "local", "colab")


def is_ollama_cloud_host(host: str) -> bool:
    hostname = (urlparse(host).hostname or "").lower()
    return hostname == "ollama.com" or hostname.endswith(".ollama.com")


class Settings:
    """Defaults from the environment; admins override the EDITABLE ones in the web
    UI (stored in the app_settings table and applied by UserStore on startup)."""

    EDITABLE = {
        "provider_backend": str,
        "api_mode": str,
        "target_language": str,
        "context_length": int,
        "ollama_model": str,
        "ollama_host": str,
        "translation_concurrency": int,
        "rpm_limit": int,
        "rpd_limit": int,
        "colab_enabled": _parse_bool,
        "colab_gpu": str,
        "colab_high_mem": _parse_bool,
        "colab_engine": str,
        "colab_model": str,
        "colab_extra_args": str,
        "colab_hf_token": str,
        "colab_auto_start": _parse_bool,
        "colab_auto_stop": _parse_bool,
        "colab_idle_stop_minutes": int,
        "colab_session_name": str,
    }

    def __init__(self) -> None:
        self.api_mode = os.getenv("LLM_API_MODE", "ollama").strip().lower()
        if self.api_mode not in {"ollama", "openai"}:
            self.api_mode = "ollama"
        self.target_language = (
            os.getenv("TARGET_LANGUAGE", "English").strip() or "English"
        )
        # 0 = auto (sized per Colab GPU, or model default for cloud/remote).
        self.context_length = int(os.getenv("LLM_CONTEXT_LENGTH", "0"))
        self.ollama_api_key = os.getenv("OLLAMA_API_KEY", "")
        self.ollama_host = os.getenv("OLLAMA_HOST", "https://ollama.com").rstrip("/")
        self.ollama_model = os.getenv("OLLAMA_MODEL", "gemma4:31b")
        self.pixiv_refresh_token = os.getenv("PIXIV_REFRESH_TOKEN", "")
        self.output_dir = Path(os.getenv("FANFICTL_OUTPUT_DIR", "./output")).resolve()
        self.app_base_url = os.getenv("APP_BASE_URL", "http://localhost:8000")
        # Empty = generated once and stored in the output dir (see webapp).
        self.app_secret_key = os.getenv("APP_SECRET_KEY", "")
        # Off by default: meant for local / Tailscale-only use, everyone is the admin.
        self.require_login = _parse_bool(os.getenv("REQUIRE_LOGIN", ""))
        self.admin_username = os.getenv("ADMIN_USERNAME", "admin")
        self.admin_password = os.getenv("ADMIN_PASSWORD", "admin")
        self.host = os.getenv("HOST", "0.0.0.0")
        self.port = int(os.getenv("PORT", "8000"))
        # Optional local caps per key; 0 = no cap (Ollama Cloud answers 429 when
        # the plan limit is hit, and that key then cools down for a minute).
        self.rpm_limit = int(os.getenv("OLLAMA_RPM_LIMIT", "0"))
        self.rpd_limit = int(os.getenv("OLLAMA_RPD_LIMIT", "0"))
        # Chapters translated in parallel (chunks within a chapter stay sequential).
        self.translation_concurrency = int(os.getenv("TRANSLATION_CONCURRENCY", "4"))

        # Managed Google Colab GPU runtime settings (via google-colab-cli + ssh -L).
        self.colab_enabled = _parse_bool(os.getenv("COLAB_ENABLED", ""))
        self.colab_gpu = os.getenv("COLAB_GPU", "L4").strip().upper() or "L4"
        self.colab_high_mem = _parse_bool(os.getenv("COLAB_HIGH_MEM", ""))
        self.colab_engine = os.getenv("COLAB_ENGINE", "vllm").strip().lower()
        if self.colab_engine not in {"vllm", "ollama"}:
            self.colab_engine = "vllm"
        self.colab_model = os.getenv("COLAB_MODEL", "").strip()
        self.colab_extra_args = os.getenv("COLAB_EXTRA_ARGS", "").strip()
        self.colab_hf_token = os.getenv("COLAB_HF_TOKEN", "").strip()
        self.colab_auto_start = _parse_bool(os.getenv("COLAB_AUTO_START", "true"))
        self.colab_auto_stop = _parse_bool(os.getenv("COLAB_AUTO_STOP", "true"))
        self.colab_idle_stop_minutes = int(os.getenv("COLAB_IDLE_STOP_MINUTES", "0"))
        self.colab_session_name = (
            os.getenv("COLAB_SESSION_NAME", "fableport").strip() or "fableport"
        )
        self.colab_local_port = int(os.getenv("COLAB_LOCAL_PORT", "8001"))

        env_backend = os.getenv("TRANSLATION_BACKEND", "").strip().lower()
        if env_backend in VALID_BACKENDS:
            self.provider_backend = env_backend
            self.colab_enabled = env_backend == "colab"
        else:
            self.provider_backend = self._infer_provider_backend()

    def _infer_provider_backend(self) -> str:
        if self.colab_enabled:
            return "colab"
        if is_ollama_cloud_host(self.ollama_host):
            return "ollama_cloud"
        return "local"

    def apply_overrides(self, values: dict[str, str]) -> None:
        for name, cast in self.EDITABLE.items():
            if name not in values or values[name] is None:
                continue
            raw = values[name]
            if raw == "" and name not in _ALLOW_EMPTY_OVERRIDES:
                continue
            setattr(self, name, cast(raw))
        if "provider_backend" in values and values["provider_backend"] in VALID_BACKENDS:
            self.colab_enabled = self.provider_backend == "colab"
        else:
            self.provider_backend = self._infer_provider_backend()

    @property
    def uses_default_admin_credentials(self) -> bool:
        return self.admin_username == "admin" and self.admin_password == "admin"

    @property
    def requires_api_key(self) -> bool:
        """Ollama Cloud requires an API key; local/vLLM/OpenAI-compatible and
        managed Colab endpoints treat API keys as optional."""
        if self.colab_enabled or self.provider_backend == "colab":
            return False
        if self.provider_backend == "ollama_cloud":
            return True
        return is_ollama_cloud_host(self.ollama_host)

    @property
    def effective_colab_model(self) -> str:
        if self.colab_model:
            return self.colab_model
        if self.colab_engine == "ollama":
            return "gemma4:26b"
        return "Qwen/Qwen2.5-14B-Instruct-AWQ"
