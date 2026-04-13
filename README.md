# fanfictl

CLI tool for fetching public Pixiv novels and translating them into English with Google AI Studio.

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
```

## Docker workflow

```bash
docker compose build
docker compose run --rm cli info "https://www.pixiv.net/novel/show.php?id=27402134"
docker compose run --rm cli translate "https://www.pixiv.net/novel/show.php?id=27402134"
```
