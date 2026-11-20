from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fanfictl.colab import (
    COLAB_SCOPES,
    REMOTE_REDIRECT_URI,
    ColabManager,
    build_remote_bootstrap_script,
    create_colab_oauth_session,
    extract_colab_code,
    normalize_colab_model_name,
    parse_usage_output,
    validate_adc_payload,
)
from fanfictl.config import Settings


class ColabUnitTests(unittest.TestCase):
    def test_oauth_url_uses_remote_redirect_and_required_scopes(self) -> None:
        with patch(
            "fanfictl.colab._load_bundled_oauth_client",
            return_value=("test-client-id.apps.googleusercontent.com", "test-secret"),
        ):
            verifier, state, url = create_colab_oauth_session()
        self.assertTrue(len(verifier) >= 32)
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        self.assertEqual(params["redirect_uri"], [REMOTE_REDIRECT_URI])
        self.assertEqual(params["token_usage"], ["remote"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["state"], [state])
        scopes = set(params["scope"][0].split())
        for required in COLAB_SCOPES:
            self.assertIn(required, scopes)

    def test_extract_colab_code(self) -> None:
        self.assertEqual(extract_colab_code("  4/0A-abc123  "), "4/0A-abc123")
        self.assertEqual(
            extract_colab_code(
                "https://sdk.cloud.google.com/applicationdefaultauthcode.html?code=4/0A-xyz"
            ),
            "4/0A-xyz",
        )
        self.assertIsNone(extract_colab_code("https://example.com/no-code"))

    def test_adc_validation_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            manager = ColabManager(settings)
            self.assertFalse(manager.has_adc_credentials())

            raw = json.dumps(
                {
                    "client_id": "12345-test.apps.googleusercontent.com",
                    "client_secret": "secret-val",
                    "refresh_token": "1//refresh-token",
                    "scopes": COLAB_SCOPES,
                }
            )
            manager.save_adc_credentials(raw)
            self.assertTrue(manager.has_adc_credentials())
            saved = json.loads(manager.adc_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["type"], "authorized_user")
            self.assertIn("12345", manager.auth_hint() or "")

            with self.assertRaises(ValueError):
                validate_adc_payload('{"type": "authorized_user"}')

            manager.clear_adc_credentials()
            self.assertFalse(manager.has_adc_credentials())

    def test_google_auth_loads_saved_adc_file_when_installed(self) -> None:
        try:
            import google.auth
        except ImportError:
            self.skipTest("google-auth not installed outside Docker colab extra")

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            manager = ColabManager(settings)
            manager.save_adc_credentials(
                {
                    "type": "authorized_user",
                    "client_id": "12345-test.apps.googleusercontent.com",
                    "client_secret": "secret-val",
                    "refresh_token": "1//refresh-token",
                    "scopes": COLAB_SCOPES,
                }
            )
            creds, _ = google.auth.load_credentials_from_file(str(manager.adc_path))
            self.assertEqual(creds.refresh_token, "1//refresh-token")

    def test_bootstrap_scripts_for_vllm_and_ollama(self) -> None:
        vllm_auto_l4 = build_remote_bootstrap_script(
            engine="vllm",
            model_name="Qwen/Qwen2.5-14B-Instruct-AWQ",
            gpu="L4",
        )
        self.assertIn("--max-model-len 32768", vllm_auto_l4)
        self.assertIn("--gpu-memory-utilization 0.90", vllm_auto_l4)

        vllm_auto_a100 = build_remote_bootstrap_script(
            engine="vllm",
            model_name="google/gemma-4-26B-A4B-it",
            gpu="A100",
            concurrency=6,
        )
        self.assertIn("--max-model-len 262144", vllm_auto_a100)
        self.assertIn("--kv-cache-dtype fp8", vllm_auto_a100)
        self.assertIn("--max-num-seqs 16", vllm_auto_a100)

        ollama_a100_4 = build_remote_bootstrap_script(
            engine="ollama",
            model_name="gemma4:26b",
            gpu="A100",
            concurrency=4,
        )
        self.assertIn("OLLAMA_NUM_PARALLEL=4", ollama_a100_4)
        self.assertIn("OLLAMA_MAX_QUEUE=512", ollama_a100_4)
        self.assertIn("OLLAMA_CONTEXT_LENGTH=262144", ollama_a100_4)
        self.assertIn("apt-get install -y -qq zstd pciutils", ollama_a100_4)

        ollama_a100_8 = build_remote_bootstrap_script(
            engine="ollama",
            model_name="gemma4:26b",
            gpu="A100",
            concurrency=8,
        )
        self.assertIn("OLLAMA_NUM_PARALLEL=8", ollama_a100_8)
        self.assertIn("OLLAMA_CONTEXT_LENGTH=131072", ollama_a100_8)

        vllm_auto_t4 = build_remote_bootstrap_script(
            engine="vllm",
            model_name="Qwen/Qwen2.5-7B-Instruct",
            gpu="T4",
        )
        self.assertIn("--dtype half", vllm_auto_t4)
        self.assertIn("--max-model-len 16384", vllm_auto_t4)

        vllm_script = build_remote_bootstrap_script(
            engine="vllm",
            model_name="google/gemma-4-31b-it",
            extra_args="--max-model-len 16384 --gpu-memory-utilization 0.9",
            hf_token="hf_test_123",
        )
        self.assertIn("vllm serve google/gemma-4-31b-it --host 127.0.0.1 --port 8000", vllm_script)
        self.assertIn("--max-model-len 16384", vllm_script)
        self.assertIn("/tmp/fableport-hf.env", vllm_script)

        ollama_script = build_remote_bootstrap_script(
            engine="ollama",
            model_name="gemma4:31b",
            gpu="L4",
            concurrency=4,
            context_length=65536,
        )
        self.assertIn("OLLAMA_NUM_PARALLEL=4", ollama_script)
        self.assertIn("OLLAMA_CONTEXT_LENGTH=65536", ollama_script)
        self.assertIn("OLLAMA_FLASH_ATTENTION=1", ollama_script)
        self.assertIn("OLLAMA_KV_CACHE_TYPE=q8_0", ollama_script)
        self.assertIn("ollama pull", ollama_script)
        self.assertIn("__FABLEPORT_PULL_FAILED__", ollama_script)
        self.assertIn(">/dev/null 2>&1 </dev/null &", ollama_script)

        hf_url_ollama = build_remote_bootstrap_script(
            engine="ollama",
            model_name="https://huggingface.co/example-org/example-model-GGUF",
            gpu="A100",
        )
        self.assertIn(
            "MODEL=hf.co/example-org/example-model-GGUF",
            hf_url_ollama,
        )

    def test_normalize_colab_model_name(self) -> None:
        self.assertEqual(
            normalize_colab_model_name(
                "https://huggingface.co/example-org/example-model-GGUF",
                "ollama",
            ),
            "hf.co/example-org/example-model-GGUF",
        )
        self.assertEqual(
            normalize_colab_model_name(
                "https://huggingface.co/user/repo/tree/main?download=true",
                "ollama",
            ),
            "hf.co/user/repo",
        )
        self.assertEqual(
            normalize_colab_model_name(
                "https://hf.co/user/repo:Q4_K_M",
                "ollama",
            ),
            "hf.co/user/repo:Q4_K_M",
        )
        self.assertEqual(
            normalize_colab_model_name(
                "huggingface.co/user/repo",
                "ollama",
            ),
            "hf.co/user/repo",
        )
        self.assertEqual(
            normalize_colab_model_name(
                "https://huggingface.co/google/gemma-4-26B-A4B-it/tree/main",
                "vllm",
            ),
            "google/gemma-4-26B-A4B-it",
        )
        self.assertEqual(
            normalize_colab_model_name("hf.co/user/repo:Q4_K_M", "vllm"),
            "user/repo",
        )
        self.assertEqual(
            normalize_colab_model_name("gemma4:26b", "ollama"),
            "gemma4:26b",
        )

    def test_stop_immediately_aborts_ensure_ready_and_allows_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.colab_engine = "ollama"
            settings.colab_model = "gemma4:26b"
            manager = ColabManager(settings)
            manager.save_adc_credentials(
                {
                    "type": "authorized_user",
                    "client_id": "12345-test.apps.googleusercontent.com",
                    "client_secret": "secret-val",
                    "refresh_token": "1//refresh-token",
                    "scopes": COLAB_SCOPES,
                }
            )
            tunnel_entered = threading.Event()
            second_attempt_ready = threading.Event()
            attempts = {"count": 0}

            def fake_start_tunnel(*, session_name: str, remote_port: int, key_path: Path) -> None:
                attempts["count"] += 1
                tunnel_entered.set()

            def fake_check_ready(*, engine: str, model_name: str) -> bool:
                if attempts["count"] >= 2:
                    second_attempt_ready.set()
                    return True
                return False

            ok_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            errors: list[Exception] = []

            with (
                patch.object(manager, "is_cli_available", return_value=True),
                patch.object(manager, "ensure_ssh_key", return_value=Path(tmp) / "id_ed25519"),
                patch.object(manager, "_remote_session_exists", return_value=True),
                patch.object(manager, "_run_colab", return_value=ok_proc),
                patch.object(manager, "_start_tunnel", side_effect=fake_start_tunnel),
                patch.object(manager, "_tunnel_alive", return_value=True),
                patch.object(manager, "_check_endpoint_ready", side_effect=fake_check_ready),
                patch.object(manager, "_fetch_remote_log_tail", return_value=(None, False)),
            ):
                def run_first() -> None:
                    try:
                        manager.ensure_ready(wait_timeout=60.0)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)

                t = threading.Thread(target=run_first, daemon=True)
                t.start()
                self.assertTrue(tunnel_entered.wait(timeout=5.0))

                t0 = time.monotonic()
                manager.stop()
                t.join(timeout=5.0)
                elapsed = time.monotonic() - t0

                self.assertFalse(t.is_alive())
                self.assertLess(elapsed, 2.0)
                self.assertEqual(len(errors), 1)
                self.assertIn("cancelled", str(errors[0]).lower())
                self.assertEqual(manager.status().state, "stopped")

                # A subsequent start_async() must not be stuck or ignored.
                manager.start_async()
                self.assertTrue(second_attempt_ready.wait(timeout=5.0))
                if manager._bg_thread:
                    manager._bg_thread.join(timeout=5.0)
                self.assertEqual(manager.status().state, "ready")

    def test_pull_failure_aborts_startup_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.colab_engine = "ollama"
            settings.colab_model = "invalid/model"
            manager = ColabManager(settings)
            manager.save_adc_credentials(
                {
                    "type": "authorized_user",
                    "client_id": "12345-test.apps.googleusercontent.com",
                    "client_secret": "secret-val",
                    "refresh_token": "1//refresh-token",
                    "scopes": COLAB_SCOPES,
                }
            )
            ok_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            with (
                patch.object(manager, "is_cli_available", return_value=True),
                patch.object(manager, "ensure_ssh_key", return_value=Path(tmp) / "id_ed25519"),
                patch.object(manager, "_remote_session_exists", return_value=True),
                patch.object(manager, "_run_colab", return_value=ok_proc),
                patch.object(manager, "_start_tunnel"),
                patch.object(manager, "_tunnel_alive", return_value=True),
                patch.object(manager, "_check_endpoint_ready", return_value=False),
                patch.object(
                    manager,
                    "_fetch_remote_log_tail",
                    return_value=("Error: invalid model name", True),
                ),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    manager.ensure_ready(wait_timeout=60.0)
                self.assertIn("invalid model name", str(ctx.exception))
                self.assertEqual(manager.status().state, "error")

    def test_fetch_remote_log_tail_multiplexes_over_control_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            manager = ColabManager(settings)
            ctl = Path(tmp) / "colab.ctl"
            ctl.write_text("", encoding="utf-8")
            manager.ssh_control_path = ctl

            fake_res = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="\x1b[?25lpulling manifest\r\x1b[Kpulling abc123... 42% 7.0 GB/16 GB\n",
                stderr="",
            )
            with (
                patch.object(manager, "_tunnel_alive", return_value=True),
                patch.object(manager, "_run_colab") as run_colab_mock,
                patch("fanfictl.colab.subprocess.run", return_value=fake_res) as run_mock,
            ):
                tail, failed = manager._fetch_remote_log_tail(
                    "fableport", Path(tmp) / "id_ed25519", engine="ollama"
                )
            self.assertFalse(failed)
            self.assertEqual(tail, "pulling abc123... 42% 7.0 GB/16 GB")
            run_colab_mock.assert_not_called()
            called_cmd = run_mock.call_args.args[0]
            self.assertIn("-S", called_cmd)
            self.assertIn(str(ctl), called_cmd)
            self.assertIn("ControlMaster=no", called_cmd)

    def test_parse_usage_output(self) -> None:
        info = parse_usage_output(
            "Current balance: 87.50 compute units\n"
            "Usage rate: 2.10/hr\n"
            "Active assignments: 1\n"
        )
        self.assertEqual(info.balance, "87.50 compute units")
        self.assertEqual(info.usage_rate, "2.10/hr")
        self.assertEqual(info.active_assignments, "1")


if __name__ == "__main__":
    unittest.main()
