# fanfictl

CLI tool for fetching public Pixiv novels and translating them into English with Google AI Studio.

It now ships with:
- a CLI
- a small FastAPI web UI
- public reader pages for finished works
- simple env-bootstrapped admin access that can be replaced later

## Local workflow

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .

fanfictl info "https://www.pixiv.net/novel/show.php?id=27402134"
fanfictl translate "https://www.pixiv.net/novel/show.php?id=27402134"
```

## Environment

Copy `.env.example` to `.env` and fill in `GEMINI_API_KEY`.

```env
GEMINI_API_KEY=...
GEMINI_MODEL=gemma-4-31b-it
FANFICTL_OUTPUT_DIR=./output
APP_BASE_URL=https://example.com
APP_DOMAIN=example.com
APP_SECRET_KEY=change-me-secret
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
HOST=0.0.0.0
PORT=8000
```

## Web UI

```bash
.venv/bin/fanfictl-web
```

Open `http://localhost:8000` and sign in with the admin credentials from `.env`.

For deployment, set your real domain only in `.env`, not in tracked files.

## Docker workflow

```bash
docker compose build
docker compose run --rm cli info "https://www.pixiv.net/novel/show.php?id=27402134"
docker compose run --rm cli translate "https://www.pixiv.net/novel/show.php?id=27402134"
docker compose up -d app
```
