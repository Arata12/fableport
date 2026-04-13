from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


class Settings:
    def __init__(self) -> None:
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "")
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemma-4-31b-it")
        self.output_dir = Path(os.getenv("FANFICTL_OUTPUT_DIR", "./output")).resolve()
        self.app_base_url = os.getenv("APP_BASE_URL", "http://localhost:8000")
        self.app_secret_key = os.getenv("APP_SECRET_KEY", "change-me-secret")
        self.admin_username = os.getenv("ADMIN_USERNAME", "admin")
        self.admin_password = os.getenv("ADMIN_PASSWORD", "admin")
        self.host = os.getenv("HOST", "0.0.0.0")
        self.port = int(os.getenv("PORT", "8000"))

    @property
    def uses_default_admin_credentials(self) -> bool:
        return self.admin_username == "admin" and self.admin_password == "admin"
