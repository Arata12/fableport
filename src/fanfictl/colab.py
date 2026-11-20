from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
from typing import Callable, Literal

import httpx

from fanfictl.config import Settings
from fanfictl.storage import atomic_write_text


AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REMOTE_REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"

COLAB_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
]

VALID_GPUS = ("T4", "L4", "A100", "H100", "G4")
VALID_ENGINES = ("vllm", "ollama")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

ProgressCallback = Callable[[str, int, int, str], None]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a subprocess and its entire process group (including ssh ProxyCommand)."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=3)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        pass


def _load_bundled_oauth_client() -> tuple[str, str]:
    """Load the OAuth2 client bundled by google-colab-cli (or env overrides)."""
    env_cid = os.environ.get("COLAB_OAUTH_CLIENT_ID", "").strip()
    env_csecret = os.environ.get("COLAB_OAUTH_CLIENT_SECRET", "").strip()
    if env_cid and env_csecret:
        return env_cid, env_csecret
    try:
        from importlib import resources

        resource = resources.files("colab_cli").joinpath("oauth_config.json")
        if resource.is_file():
            payload = json.loads(resource.read_text(encoding="utf-8"))
            installed = payload.get("installed") or {}
            cid = installed.get("client_id")
            csecret = installed.get("client_secret")
            if cid and csecret:
                return str(cid), str(csecret)
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError(
        "google-colab-cli is not installed (unable to load bundled OAuth config). "
        "Install with `pip install '.[colab]'` or run via Docker."
    )


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def create_colab_oauth_session() -> tuple[str, str, str]:
    """Build a Google OAuth2 remote copy-paste URL identical to google-colab-cli."""
    client_id, _ = _load_bundled_oauth_client()
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": REMOTE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(COLAB_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "token_usage": "remote",
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }
    return verifier, state, f"{AUTH_URI}?{urllib.parse.urlencode(params)}"


def extract_colab_code(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if "code=" in cleaned:
        parsed = urllib.parse.urlparse(cleaned)
        query = urllib.parse.parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        return code.strip() if code else None
    if "://" in cleaned or cleaned.startswith("/"):
        return None
    return cleaned


def exchange_colab_code(*, code: str, code_verifier: str) -> dict:
    """Exchange the authorization code for an authorized_user ADC JSON dict."""
    client_id, client_secret = _load_bundled_oauth_client()
    response = httpx.post(
        TOKEN_URI,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": REMOTE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Google OAuth token exchange failed (HTTP {response.status_code}): {response.text[:300]}"
        )
    payload = response.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "Google did not return a refresh_token. Retry the login flow and grant consent."
        )
    return {
        "type": "authorized_user",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "scopes": list(COLAB_SCOPES),
    }


def validate_adc_payload(raw: dict | str) -> dict:
    """Validate and normalize an authorized_user ADC JSON document."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
    else:
        data = dict(raw)
    if not isinstance(data, dict):
        raise ValueError("ADC credentials must be a JSON object.")

    cred_type = data.get("type") or "authorized_user"
    if cred_type == "authorized_user":
        missing = [
            key
            for key in ("client_id", "client_secret", "refresh_token")
            if not str(data.get(key) or "").strip()
        ]
        if missing:
            raise ValueError(
                f"ADC credentials are missing required field(s): {', '.join(missing)}"
            )
        normalized = {
            "type": "authorized_user",
            "client_id": str(data["client_id"]).strip(),
            "client_secret": str(data["client_secret"]).strip(),
            "refresh_token": str(data["refresh_token"]).strip(),
        }
        if data.get("scopes"):
            normalized["scopes"] = list(data["scopes"])
        if data.get("quota_project_id"):
            normalized["quota_project_id"] = str(data["quota_project_id"]).strip()
        return normalized
    if cred_type == "service_account":
        if not data.get("private_key") or not data.get("client_email"):
            raise ValueError("Service account JSON is missing private_key or client_email.")
        return data
    raise ValueError(f"Unsupported ADC credential type: {cred_type}")


@dataclass
class ColabEndpoint:
    base_url: str
    api_mode: Literal["openai", "ollama"]
    model_name: str
    gpu: str
    engine: str


@dataclass
class ColabUsageInfo:
    balance: str | None = None
    usage_rate: str | None = None
    active_assignments: str | None = None
    raw_output: str = ""
    fetched_at: str | None = None
    error: str | None = None


@dataclass
class ColabStatusSnapshot:
    state: str
    detail: str
    cli_available: bool
    authenticated: bool
    auth_hint: str | None
    gpu: str
    engine: str
    model: str
    local_url: str
    session_name: str
    started_at: str | None
    last_error: str | None
    logs: list[str]
    usage: ColabUsageInfo | None


def normalize_colab_model_name(model_name: str, engine: str) -> str:
    """Normalize pasted HuggingFace URLs into the format expected by Ollama or vLLM.

    - https://huggingface.co/user/repo -> hf.co/user/repo (Ollama) or user/repo (vLLM)
    - https://hf.co/user/repo:Q5_K_M  -> hf.co/user/repo:Q5_K_M (Ollama) or user/repo (vLLM)
    """
    cleaned = (model_name or "").strip()
    if not cleaned:
        return cleaned
    lowered = cleaned.lower()
    for prefix in (
        "https://huggingface.co/",
        "http://huggingface.co/",
        "https://hf.co/",
        "http://hf.co/",
        "huggingface.co/",
    ):
        if lowered.startswith(prefix):
            rest = cleaned[len(prefix) :].split("?", 1)[0].split("#", 1)[0].strip("/")
            # Strip /tree/... or /blob/... if someone copied a file/branch URL.
            parts = rest.split("/")
            if len(parts) >= 2:
                repo = f"{parts[0]}/{parts[1]}"
            else:
                repo = rest
            if engine == "ollama":
                return f"hf.co/{repo}"
            return repo.split(":", 1)[0]
    if engine == "vllm" and lowered.startswith("hf.co/"):
        rest = cleaned.split("/", 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
        return rest.split(":", 1)[0]
    return cleaned


def resolve_colab_parallel(gpu: str, concurrency: int = 4) -> int:
    """Cap Ollama/vLLM parallel slots safely per Colab GPU."""
    normalized = (gpu or "L4").strip().upper()
    if normalized == "T4":
        cap = 2
    elif normalized == "L4":
        cap = 4
    else:
        cap = 16
    return max(1, min(int(concurrency or 1), cap))


def resolve_colab_context_length(
    gpu: str,
    configured: int = 0,
    *,
    engine: str = "vllm",
    parallel: int = 4,
) -> int:
    """Return explicit context length if configured (>0), else a GPU + parallelism-aware default.

    vLLM uses PagedAttention (shared KV pool across parallel chapters), so 256k
    works cleanly on A100/H100. Ollama pre-allocates KV cache per parallel slot,
    so on A100 40GB we use 256k up to 4 parallel chapters (e.g. Gemma 4 26B Q5
    uses ~2.6 GiB/slot in Q8_0 -> ~30 GiB total), 128k up to 8, and 64k above 8.
    """
    if configured and configured >= 4096:
        return int(configured)
    normalized = (gpu or "L4").strip().upper()
    if normalized == "T4":
        return 16384
    if normalized == "L4":
        return 32768
    if normalized == "H100":
        return 262144
    # A100 / G4 (40 GB class)
    if engine == "vllm":
        return 262144
    if parallel <= 4:
        return 262144
    if parallel <= 8:
        return 131072
    return 65536


def default_vllm_args_for_gpu(
    gpu: str,
    context_length: int = 0,
    *,
    parallel: int = 4,
) -> str:
    """Safe, ready-to-use vLLM defaults per Colab GPU when user leaves extra args empty."""
    normalized = (gpu or "L4").strip().upper()
    ctx = resolve_colab_context_length(
        normalized, context_length, engine="vllm", parallel=parallel
    )
    max_seqs = max(int(parallel or 4), 16)
    if normalized == "T4":
        return f"--dtype half --max-model-len {ctx} --max-num-seqs {max_seqs} --gpu-memory-utilization 0.90"
    if ctx >= 131072:
        return f"--max-model-len {ctx} --kv-cache-dtype fp8 --max-num-seqs {max_seqs} --gpu-memory-utilization 0.92"
    return f"--max-model-len {ctx} --max-num-seqs {max_seqs} --gpu-memory-utilization 0.90"


def build_remote_bootstrap_script(
    *,
    engine: str,
    model_name: str,
    gpu: str = "L4",
    concurrency: int = 4,
    context_length: int = 0,
    extra_args: str = "",
    hf_token: str = "",
) -> str:
    """Build an idempotent bash script executed on the remote Colab VM."""
    normalized_model = normalize_colab_model_name(model_name, engine)
    quoted_model = shlex.quote(normalized_model)
    parallel = resolve_colab_parallel(gpu, concurrency)
    ctx = resolve_colab_context_length(
        gpu, context_length, engine=engine, parallel=parallel
    )
    if engine == "ollama":
        cfg_sig = shlex.quote(f"parallel={parallel},ctx={ctx}")
        return f"""set -euo pipefail
LOG=/tmp/fableport-llm.log
MODEL={quoted_model}
CFG_FILE=/tmp/fableport-ollama-cfg.txt
WANT_CFG={cfg_sig}
: > "$LOG"
if ! command -v ollama >/dev/null 2>&1; then
  if ! command -v zstd >/dev/null 2>&1 || ! command -v lspci >/dev/null 2>&1; then
    echo "[fableport] Installing zstd & pciutils..." | tee -a "$LOG"
    apt-get update -qq >> "$LOG" 2>&1 && apt-get install -y -qq zstd pciutils >> "$LOG" 2>&1
  fi
  echo "[fableport] Installing Ollama..." | tee -a "$LOG"
  curl -fsSL https://ollama.com/install.sh | sh >> "$LOG" 2>&1
fi
NEED_START=1
if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  if [ -f "$CFG_FILE" ] && [ "$(cat "$CFG_FILE")" = "$WANT_CFG" ]; then
    NEED_START=0
  else
    echo "[fableport] Restarting Ollama with updated parallel/context config ($WANT_CFG)..." | tee -a "$LOG"
    pkill -f "ollama serve" 2>/dev/null || true
    sleep 2
  fi
fi
if [ "$NEED_START" = "1" ]; then
  echo "[fableport] Starting Ollama daemon ($WANT_CFG, flash_attn=1, kv=q8_0)..." | tee -a "$LOG"
  printf '%s' "$WANT_CFG" > "$CFG_FILE"
  nohup env \
    OLLAMA_HOST=127.0.0.1:11434 \
    OLLAMA_NUM_PARALLEL={parallel} \
    OLLAMA_MAX_QUEUE=512 \
    OLLAMA_CONTEXT_LENGTH={ctx} \
    OLLAMA_FLASH_ATTENTION=1 \
    OLLAMA_KV_CACHE_TYPE=q8_0 \
    OLLAMA_KEEP_ALIVE=30m \
    ollama serve >> "$LOG" 2>&1 < /dev/null &
  for _ in $(seq 1 30); do
    curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
  done
fi
echo "[fableport] Pulling model $MODEL..." | tee -a "$LOG"
rm -f /tmp/fableport-pull-failed
if [ -f /tmp/fableport-pull.pid ]; then
  OLD_PULL_PID="$(cat /tmp/fableport-pull.pid 2>/dev/null || true)"
  if [ -n "$OLD_PULL_PID" ] && kill -0 "$OLD_PULL_PID" 2>/dev/null; then
    kill "$OLD_PULL_PID" 2>/dev/null || true
    sleep 1
  fi
fi
nohup bash -c 'ollama pull "$1" >> "$2" 2>&1 || {{ echo "__FABLEPORT_PULL_FAILED__" >> "$2"; touch /tmp/fableport-pull-failed; }}' _ "$MODEL" "$LOG" >/dev/null 2>&1 </dev/null &
echo $! > /tmp/fableport-pull.pid
echo "[fableport] Ollama bootstrap dispatched."
"""

    effective_extra = extra_args.strip() or default_vllm_args_for_gpu(
        gpu, ctx, parallel=parallel
    )
    parsed_extra = shlex.split(effective_extra)
    extra_cli = " ".join(shlex.quote(arg) for arg in parsed_extra)
    vllm_sig = shlex.quote(f"{normalized_model}|{extra_cli}")
    hf_setup = ""
    if hf_token.strip():
        hf_setup = (
            f"umask 077\n"
            f"printf 'export HF_TOKEN=%s\\n' {shlex.quote(hf_token.strip())} > /tmp/fableport-hf.env\n"
            f"chmod 600 /tmp/fableport-hf.env\n"
        )
    else:
        hf_setup = "rm -f /tmp/fableport-hf.env\n"

    return f"""set -euo pipefail
LOG=/tmp/fableport-llm.log
MODEL={quoted_model}
CFG_FILE=/tmp/fableport-vllm-cfg.txt
WANT_CFG={vllm_sig}
{hf_setup}
if curl -sf http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
  if [ -f "$CFG_FILE" ] && [ "$(cat "$CFG_FILE")" = "$WANT_CFG" ]; then
    echo "[fableport] vLLM is already serving $MODEL." | tee -a "$LOG"
    exit 0
  fi
fi

if [ -f /tmp/fableport-vllm.pid ]; then
  OLD_PID="$(cat /tmp/fableport-vllm.pid 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
  fi
fi

cat <<'EOF' > /tmp/fableport-start-llm.sh
#!/usr/bin/env bash
set -euo pipefail
if [ -f /tmp/fableport-hf.env ]; then
  # shellcheck disable=SC1091
  source /tmp/fableport-hf.env
fi
if ! command -v vllm >/dev/null 2>&1; then
  echo "[fableport] Installing vLLM..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install --system vllm
  else
    python3 -m pip install -q vllm
  fi
fi
echo "[fableport] Starting vLLM server..."
printf '[fableport] Model: %s\n' {quoted_model}
exec vllm serve {quoted_model} --host 127.0.0.1 --port 8000 {extra_cli}
EOF
chmod +x /tmp/fableport-start-llm.sh
printf '%s' "$WANT_CFG" > "$CFG_FILE"
: > "$LOG"
nohup bash /tmp/fableport-start-llm.sh >> "$LOG" 2>&1 < /dev/null &
echo $! > /tmp/fableport-vllm.pid
echo "[fableport] vLLM bootstrap dispatched (pid $(cat /tmp/fableport-vllm.pid))."
"""


def parse_usage_output(output: str) -> ColabUsageInfo:
    info = ColabUsageInfo(raw_output=output.strip(), fetched_at=_utc_now())
    for line in output.splitlines():
        m_bal = re.search(r"Current balance:\s*(.+)", line, flags=re.IGNORECASE)
        if m_bal:
            info.balance = m_bal.group(1).strip()
        m_rate = re.search(r"Usage rate:\s*(.+)", line, flags=re.IGNORECASE)
        if m_rate:
            info.usage_rate = m_rate.group(1).strip()
        m_cnt = re.search(r"Active assignments:\s*(.+)", line, flags=re.IGNORECASE)
        if m_cnt:
            info.active_assignments = m_cnt.group(1).strip()
    return info


class ColabManager:
    """Manages a Google Colab GPU runtime + in-container SSH port forward."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.colab_dir = settings.output_dir / ".colab"
        self.adc_path = self.colab_dir / "adc.json"
        self.sessions_path = self.colab_dir / "sessions.json"
        self.ssh_key_path = self.colab_dir / "id_ed25519"
        self.ssh_control_path = Path(
            f"/tmp/fableport-colab-{os.getpid()}-{id(self)}.ctl"
        )
        self._lock = threading.Lock()
        self._startup_lock = threading.Lock()
        self._state: str = "stopped"
        self._detail: str = "Colab runtime is stopped"
        self._last_error: str | None = None
        self._started_at: str | None = None
        self._active_model: str | None = None
        self._active_engine: str | None = None
        self._active_gpu: str | None = None
        self._active_concurrency: int | None = None
        self._active_context_length: int | None = None
        self._tunnel_proc: subprocess.Popen | None = None
        self._active_cmd_procs: set[subprocess.Popen] = set()
        self._idle_timer: threading.Timer | None = None
        self._cancel_events: set[threading.Event] = set()
        self._logs: deque[str] = deque(maxlen=120)
        self._usage: ColabUsageInfo | None = None
        self._bg_thread: threading.Thread | None = None

    def _log(self, message: str) -> None:
        timestamp = datetime.now(UTC).strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        with self._lock:
            self._logs.append(line)

    def _set_state(
        self, state: str, detail: str, *, error: str | None = None
    ) -> None:
        with self._lock:
            self._state = state
            self._detail = detail
            if error is not None:
                self._last_error = error
            elif state in {"starting", "bootstrapping", "tunneling", "ready", "stopped"}:
                self._last_error = None

    def is_cli_available(self) -> bool:
        return bool(
            shutil.which("colab")
            and shutil.which("ssh")
            and shutil.which("ssh-keygen")
        )

    def has_adc_credentials(self) -> bool:
        return self.adc_path.exists() and self.adc_path.stat().st_size > 0

    def auth_hint(self) -> str | None:
        if not self.has_adc_credentials():
            return None
        try:
            data = json.loads(self.adc_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return "Stored credentials file (unreadable)"
        cred_type = data.get("type", "authorized_user")
        if cred_type == "service_account":
            return f"Service account: {data.get('client_email', 'configured')}"
        client_id = str(data.get("client_id", ""))
        short = client_id.split("-", 1)[0] if "-" in client_id else client_id[:10]
        return f"Google OAuth ADC connected (client {short}…)"

    def save_adc_credentials(self, raw: dict | str) -> None:
        normalized = validate_adc_payload(raw)
        self.colab_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.adc_path, json.dumps(normalized, indent=2))
        try:
            self.adc_path.chmod(0o600)
        except OSError:
            pass
        with self._lock:
            self._usage = None
        self._log("Saved Google Colab ADC credentials.")

    def clear_adc_credentials(self) -> None:
        if self.adc_path.exists():
            self.adc_path.unlink()
        with self._lock:
            self._usage = None
        self._log("Removed Google Colab credentials.")

    def _subprocess_env(self) -> dict[str, str]:
        self.colab_dir.mkdir(parents=True, exist_ok=True)
        config_dir = self.colab_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["GOOGLE_APPLICATION_CREDENTIALS"] = str(self.adc_path)
        env["HOME"] = str(self.colab_dir)
        env["XDG_CONFIG_HOME"] = str(config_dir)
        return env

    def _colab_base_cmd(self) -> list[str]:
        colab_bin = shutil.which("colab") or "colab"
        return [
            colab_bin,
            "--auth=adc",
            "--config",
            str(self.sessions_path),
        ]

    def _run_colab(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        timeout: float = 180.0,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [*self._colab_base_cmd(), *args]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._subprocess_env(),
            start_new_session=True,
        )
        with self._lock:
            self._active_cmd_procs.add(proc)
        try:
            stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=proc.returncode or 0,
                stdout=stdout or "",
                stderr=stderr or "",
            )
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            raise
        except Exception:
            _kill_process_tree(proc)
            raise
        finally:
            with self._lock:
                self._active_cmd_procs.discard(proc)

    def ensure_ssh_key(self) -> Path:
        self.colab_dir.mkdir(parents=True, exist_ok=True)
        if not self.ssh_key_path.exists():
            result = subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "fableport-colab",
                    "-f",
                    str(self.ssh_key_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to generate ed25519 SSH key: {result.stderr.strip() or result.stdout.strip()}"
                )
            try:
                self.ssh_key_path.chmod(0o600)
            except OSError:
                pass
        return self.ssh_key_path

    def refresh_usage(self) -> ColabUsageInfo:
        if not self.is_cli_available():
            info = ColabUsageInfo(
                error="google-colab-cli is not installed in this environment."
            )
            with self._lock:
                self._usage = info
            return info
        if not self.has_adc_credentials():
            info = ColabUsageInfo(error="Google Colab is not connected yet.")
            with self._lock:
                self._usage = info
            return info
        try:
            result = self._run_colab(["usage"], timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            info = ColabUsageInfo(error=str(exc), fetched_at=_utc_now())
            with self._lock:
                self._usage = info
            return info
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip() or "colab usage failed"
            info = ColabUsageInfo(error=err[:300], fetched_at=_utc_now())
        else:
            info = parse_usage_output(result.stdout)
        with self._lock:
            self._usage = info
        return info

    def status(self) -> ColabStatusSnapshot:
        with self._lock:
            state = self._state
            detail = self._detail
            started_at = self._started_at
            last_error = self._last_error
            logs = list(self._logs)
            usage = self._usage
            gpu = self._active_gpu or self.settings.colab_gpu
            engine = self._active_engine or self.settings.colab_engine
            model = self._active_model or normalize_colab_model_name(
                self.settings.effective_colab_model, engine
            )
        return ColabStatusSnapshot(
            state=state,
            detail=detail,
            cli_available=self.is_cli_available(),
            authenticated=self.has_adc_credentials(),
            auth_hint=self.auth_hint(),
            gpu=gpu,
            engine=engine,
            model=model,
            local_url=f"http://127.0.0.1:{self.settings.colab_local_port}",
            session_name=self.settings.colab_session_name,
            started_at=started_at,
            last_error=last_error,
            logs=logs,
            usage=usage,
        )

    def _cancel_idle_timer(self) -> None:
        with self._lock:
            timer = self._idle_timer
            self._idle_timer = None
        if timer:
            timer.cancel()

    def _tunnel_alive(self) -> bool:
        with self._lock:
            proc = self._tunnel_proc
        return proc is not None and proc.poll() is None

    def _stop_tunnel(self) -> None:
        with self._lock:
            proc = self._tunnel_proc
            self._tunnel_proc = None
        if proc is not None:
            _kill_process_tree(proc)
        try:
            self.ssh_control_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _start_tunnel(self, *, session_name: str, remote_port: int, key_path: Path) -> None:
        self._stop_tunnel()
        proxy_cmd = shlex.join(
            [
                *self._colab_base_cmd(),
                "ssh",
                "--proxy-mode",
                "-s",
                session_name,
                "-i",
                str(key_path),
            ]
        )
        local_port = self.settings.colab_local_port
        ssh_cmd = [
            "ssh",
            "-N",
            "-M",
            "-S",
            str(self.ssh_control_path),
            "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            "-i",
            str(key_path),
            "-o",
            "ControlPersist=no",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            f"ProxyCommand={proxy_cmd}",
            "root@colab-runtime",
        ]
        self._log(
            f"Opening SSH tunnel 127.0.0.1:{local_port} -> colab:{remote_port} ({session_name})..."
        )
        proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self._subprocess_env(),
            start_new_session=True,
        )
        with self._lock:
            self._tunnel_proc = proc

    def _check_endpoint_ready(self, *, engine: str, model_name: str) -> bool:
        base_url = f"http://127.0.0.1:{self.settings.colab_local_port}"
        try:
            with httpx.Client(base_url=base_url, timeout=5.0) as client:
                if engine == "ollama":
                    resp = client.get("/api/tags")
                    if resp.status_code != 200:
                        return False
                    models = [
                        str(m.get("name", ""))
                        for m in (resp.json().get("models") or [])
                        if isinstance(m, dict)
                    ]
                    if not models:
                        return False
                    # Match exact name or tagless prefix, case-insensitive (handles hf.co/...:Q5_K_M).
                    wanted = model_name.lower()
                    return any(
                        m.lower() == wanted or m.lower().split(":", 1)[0] == wanted
                        for m in models
                    )
                resp = client.get("/v1/models")
                if resp.status_code != 200:
                    return False
                models = [
                    str(m.get("id", ""))
                    for m in (resp.json().get("data") or [])
                    if isinstance(m, dict)
                ]
                return bool(models)
        except Exception:  # noqa: BLE001
            return False

    def _remote_session_exists(self, session_name: str) -> bool:
        result = self._run_colab(["status", "-s", session_name], timeout=30.0)
        if result.returncode != 0:
            return False
        combined = f"{result.stdout}\n{result.stderr}"
        return "not found" not in combined.lower() and bool(result.stdout.strip())

    def _fetch_remote_log_tail(
        self, session_name: str, key_path: Path, *, engine: str = "ollama"
    ) -> tuple[str | None, bool]:
        # Reuse the active SSH tunnel's ControlMaster socket so we never open a
        # second `colab ssh` proxy session (Colab rejects concurrent SSH proxies
        # with HTTP 429).
        if not (self._tunnel_alive() and self.ssh_control_path.exists()):
            return None, False
        if engine == "vllm":
            probe_cmd = (
                "tail -n 10 /tmp/fableport-llm.log 2>/dev/null; "
                '([ -f /tmp/fableport-vllm.pid ] && ! kill -0 "$(cat /tmp/fableport-vllm.pid)" 2>/dev/null) '
                "&& echo __FABLEPORT_PULL_FAILED__ || true"
            )
        else:
            probe_cmd = (
                "tail -n 10 /tmp/fableport-llm.log 2>/dev/null; "
                "[ -f /tmp/fableport-pull-failed ] && echo __FABLEPORT_PULL_FAILED__ || true"
            )
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-S",
                    str(self.ssh_control_path),
                    "-o",
                    "ControlMaster=no",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "LogLevel=ERROR",
                    "root@colab-runtime",
                    probe_cmd,
                ],
                capture_output=True,
                text=True,
                env=self._subprocess_env(),
                timeout=15.0,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                raw_lines = [
                    _ANSI_ESCAPE_RE.sub("", ln).strip()
                    for ln in result.stdout.splitlines()
                ]
                lines = [ln for ln in raw_lines if ln]
                failed = any("__FABLEPORT_PULL_FAILED__" in ln for ln in lines)
                clean = [ln for ln in lines if "__FABLEPORT_PULL_FAILED__" not in ln]
                return (clean[-1] if clean else None), failed
        except Exception:  # noqa: BLE001
            return None, False
        return None, False

    def ensure_ready(
        self,
        *,
        progress_callback: ProgressCallback | None = None,
        wait_timeout: float = 900.0,
        _cancel_event: threading.Event | None = None,
    ) -> ColabEndpoint:
        self._cancel_idle_timer()

        def report(detail: str) -> None:
            if progress_callback:
                progress_callback("colab", 0, 0, detail)

        engine = self.settings.colab_engine
        model_name = normalize_colab_model_name(
            self.settings.effective_colab_model, engine
        )
        gpu = self.settings.colab_gpu
        session_name = self.settings.colab_session_name
        local_url = f"http://127.0.0.1:{self.settings.colab_local_port}"
        api_mode: Literal["openai", "ollama"] = (
            "openai" if engine == "vllm" else "ollama"
        )

        # Fast path: already ready with the same engine, model, parallelism & context.
        want_concurrency = self.settings.translation_concurrency
        want_ctx = self.settings.context_length
        with self._lock:
            already_matches = (
                self._state == "ready"
                and self._active_engine == engine
                and self._active_model == model_name
                and self._active_concurrency == want_concurrency
                and self._active_context_length == want_ctx
            )
        if (
            already_matches
            and self._tunnel_alive()
            and self._check_endpoint_ready(engine=engine, model_name=model_name)
        ):
            if _cancel_event is not None:
                with self._lock:
                    self._cancel_events.discard(_cancel_event)
            return ColabEndpoint(
                base_url=local_url,
                api_mode=api_mode,
                model_name=model_name,
                gpu=gpu,
                engine=engine,
            )

        cancel_event = _cancel_event
        if cancel_event is None:
            cancel_event = threading.Event()
            with self._lock:
                self._cancel_events.add(cancel_event)

        def check_cancelled() -> None:
            if cancel_event.is_set():
                raise RuntimeError("Colab startup cancelled.")

        try:
            with self._startup_lock:
                check_cancelled()
                self._cancel_idle_timer()
                if (
                    self._state == "ready"
                    and self._active_engine == engine
                    and self._active_model == model_name
                    and self._active_concurrency == want_concurrency
                    and self._active_context_length == want_ctx
                    and self._tunnel_alive()
                    and self._check_endpoint_ready(engine=engine, model_name=model_name)
                ):
                    return ColabEndpoint(
                        base_url=local_url,
                        api_mode=api_mode,
                        model_name=model_name,
                        gpu=gpu,
                        engine=engine,
                    )

                if not self.is_cli_available():
                    msg = "google-colab-cli / openssh-client not found. Run Fableport via Docker or install google-colab-cli + openssh."
                    self._set_state("error", msg, error=msg)
                    raise RuntimeError(msg)
                if not self.has_adc_credentials():
                    msg = "Google Colab is not authenticated. Connect your Google account in Settings -> Google Colab."
                    self._set_state("error", msg, error=msg)
                    raise RuntimeError(msg)

                try:
                    key_path = self.ensure_ssh_key()
                    check_cancelled()
                    self._set_state(
                        "starting",
                        f"Provisioning Colab {gpu} session '{session_name}'...",
                    )
                    report(f"Provisioning Colab {gpu} runtime...")
                    self._log(
                        f"Checking Colab session '{session_name}' ({gpu}, engine={engine}, model={model_name})."
                    )

                    if not self._remote_session_exists(session_name):
                        check_cancelled()
                        new_args = ["new", "-s", session_name, "--gpu", gpu]
                        if self.settings.colab_high_mem and gpu != "L4":
                            new_args.append("--high-mem")
                        self._log(f"Running: colab {' '.join(new_args)}")
                        res = self._run_colab(new_args, timeout=180.0)
                        check_cancelled()
                        if res.returncode != 0:
                            err = (res.stderr or res.stdout).strip() or "Failed to create Colab session"
                            raise RuntimeError(f"colab new failed: {err[:400]}")
                        self._log(res.stdout.strip() or f"Session '{session_name}' allocated.")
                    else:
                        check_cancelled()
                        self._log(f"Reusing active Colab session '{session_name}'.")

                    self._stop_tunnel()
                    self._set_state(
                        "bootstrapping",
                        f"Starting {engine} ({model_name}) on Colab {gpu}...",
                    )
                    report(f"Bootstrapping {engine} ({model_name}) on Colab...")
                    bootstrap = build_remote_bootstrap_script(
                        engine=engine,
                        model_name=model_name,
                        gpu=gpu,
                        concurrency=self.settings.translation_concurrency,
                        context_length=self.settings.context_length,
                        extra_args=self.settings.colab_extra_args,
                        hf_token=self.settings.colab_hf_token,
                    )
                    boot_res = None
                    for attempt in range(3):
                        boot_res = self._run_colab(
                            ["ssh", "-s", session_name, "-i", str(key_path), "--", "bash", "-s"],
                            input_text=bootstrap,
                            timeout=300.0,
                        )
                        check_cancelled()
                        if boot_res.returncode == 0:
                            break
                        err_text = (boot_res.stderr or boot_res.stdout).lower()
                        if attempt < 2 and ("429" in err_text or "already-active ssh" in err_text):
                            self._log("Previous Colab SSH session still releasing; retrying bootstrap in 3s...")
                            if cancel_event.wait(3.0):
                                check_cancelled()
                            continue
                        break
                    assert boot_res is not None
                    if boot_res.returncode != 0:
                        err = (boot_res.stderr or boot_res.stdout).strip() or "Bootstrap failed"
                        raise RuntimeError(f"Remote {engine} bootstrap failed: {err[:400]}")
                    for line in (boot_res.stdout or "").splitlines():
                        if line.strip():
                            self._log(line.strip())

                    remote_port = 11434 if engine == "ollama" else 8000
                    self._set_state(
                        "tunneling",
                        f"Waiting for {engine} ({model_name}) over SSH tunnel...",
                    )
                    report(f"Waiting for {engine} model to finish loading...")
                    self._start_tunnel(
                        session_name=session_name,
                        remote_port=remote_port,
                        key_path=key_path,
                    )

                    deadline = time.monotonic() + wait_timeout
                    last_tail_check = 0.0
                    tunnel_restarts = 0
                    while time.monotonic() < deadline:
                        check_cancelled()
                        if not self._tunnel_alive():
                            tunnel_restarts += 1
                            if tunnel_restarts > 5:
                                raise RuntimeError(
                                    "SSH port-forwarding tunnel exited repeatedly. Check Colab session status."
                                )
                            self._log("SSH tunnel closed while waiting; reconnecting...")
                            if cancel_event.wait(2.0):
                                check_cancelled()
                            self._start_tunnel(
                                session_name=session_name,
                                remote_port=remote_port,
                                key_path=key_path,
                            )
                        if self._check_endpoint_ready(engine=engine, model_name=model_name):
                            check_cancelled()
                            with self._lock:
                                self._started_at = self._started_at or _utc_now()
                                self._active_engine = engine
                                self._active_model = model_name
                                self._active_gpu = gpu
                                self._active_concurrency = want_concurrency
                                self._active_context_length = want_ctx
                            self._set_state(
                                "ready",
                                f"Colab {gpu} ready ({engine} · {model_name})",
                            )
                            self._log(
                                f"Endpoint ready at {local_url} ({engine} · {model_name})."
                            )
                            report(f"Colab {gpu} ready ({model_name})")
                            return ColabEndpoint(
                                base_url=local_url,
                                api_mode=api_mode,
                                model_name=model_name,
                                gpu=gpu,
                                engine=engine,
                            )
                        now = time.monotonic()
                        if now - last_tail_check >= 6.0:
                            last_tail_check = now
                            tail, pull_failed = self._fetch_remote_log_tail(
                                session_name, key_path, engine=engine
                            )
                            check_cancelled()
                            if tail:
                                self._log(f"[remote] {tail[:200]}")
                                self._set_state(
                                    "tunneling", f"Loading {model_name}: {tail[:120]}"
                                )
                                report(f"Colab: {tail[:120]}")
                            if pull_failed:
                                raise RuntimeError(
                                    f"Model startup/download failed on Colab for '{model_name}': {tail or 'check model name and logs'}"
                                )
                        if cancel_event.wait(3.0):
                            check_cancelled()

                    raise RuntimeError(
                        f"Timed out waiting for {engine} ({model_name}) to become ready on Colab."
                    )
                except Exception as exc:  # noqa: BLE001
                    self._stop_tunnel()
                    if cancel_event.is_set():
                        self._log("Colab startup cancelled.")
                        raise
                    self._log(f"Error: {exc}")
                    self._set_state("error", str(exc), error=str(exc))
                    raise
        finally:
            with self._lock:
                self._cancel_events.discard(cancel_event)

    def start_async(self) -> None:
        with self._lock:
            if self._state in {"starting", "bootstrapping", "tunneling", "ready"}:
                return
            self._state = "starting"
            self._detail = f"Starting Colab {self.settings.colab_gpu} runtime..."
            self._last_error = None
            cancel_event = threading.Event()
            self._cancel_events.add(cancel_event)

            def runner() -> None:
                try:
                    self.ensure_ready(_cancel_event=cancel_event)
                except Exception:  # noqa: BLE001
                    pass

            self._bg_thread = threading.Thread(target=runner, daemon=True)
            self._bg_thread.start()

    def stop(self) -> None:
        with self._lock:
            for ev in list(self._cancel_events):
                ev.set()
            running_cmds = list(self._active_cmd_procs)
        self._cancel_idle_timer()
        session_name = self.settings.colab_session_name
        self._set_state("stopping", f"Stopping Colab session '{session_name}'...")
        self._log(f"Stopping Colab session '{session_name}'...")
        for proc in running_cmds:
            _kill_process_tree(proc)
        self._stop_tunnel()
        if self.is_cli_available() and self.has_adc_credentials():
            try:
                res = self._run_colab(["stop", "-s", session_name], timeout=60.0)
                output = (res.stdout or res.stderr or "").strip()
                if output:
                    self._log(output[:200])
            except Exception as exc:  # noqa: BLE001
                self._log(f"Warning while stopping session: {exc}")
        with self._lock:
            self._started_at = None
            self._active_model = None
            self._active_engine = None
            self._active_gpu = None
            self._active_concurrency = None
            self._active_context_length = None
        self._set_state("stopped", "Colab runtime is stopped")

    def schedule_idle_stop(self, has_pending_work: Callable[[], bool]) -> None:
        if not self.settings.colab_auto_stop:
            return
        with self._lock:
            if self._state not in {"ready", "starting", "bootstrapping", "tunneling"}:
                return
        self._cancel_idle_timer()
        delay_seconds = max(0, int(self.settings.colab_idle_stop_minutes)) * 60
        if delay_seconds == 0:
            if not has_pending_work():
                self._log("Translation queue empty; auto-stopping Colab runtime.")
                self.stop()
            return

        def maybe_stop() -> None:
            if not has_pending_work():
                self._log(
                    f"Colab idle for {self.settings.colab_idle_stop_minutes} min; auto-stopping runtime."
                )
                self.stop()

        timer = threading.Timer(delay_seconds, maybe_stop)
        timer.daemon = True
        with self._lock:
            self._idle_timer = timer
        self._log(
            f"Scheduled Colab auto-stop in {self.settings.colab_idle_stop_minutes} min if idle."
        )
        timer.start()

    def shutdown(self) -> None:
        """Clean up tunnel and auto-stop managed runtime on app shutdown."""
        self._cancel_idle_timer()
        with self._lock:
            active = self._state in {"ready", "starting", "bootstrapping", "tunneling"}
        if active and self.settings.colab_auto_stop:
            self.stop()
        else:
            self._stop_tunnel()
