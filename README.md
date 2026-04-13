# fanfictl

Docker-first Pixiv fanfic translator and reader.

It can:
- fetch public Pixiv novels and series
- translate them to English with Google AI Studio using `gemma-4-31b-it`
- store canonical Markdown plus exports
- serve a small web UI with admin submission and public reader links
- track Gemini request quotas
- use one `.env` key plus extra fallback keys added from the dashboard

## Main features

- **CLI** for fetch/translate/export
- **Web UI** for submissions, jobs, library management, and public reading
- **Public reader links** for completed works
- **Exports**: Markdown, TXT, HTML, EPUB
- **Checkpoint/resume** support
- **Gemini quota tracking**
- **Fallback API keys** stored in the app volume
- **Docker + Caddy labels** for deployment

## Project docs

- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- [docs/USAGE.md](docs/USAGE.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Quick start

1. Copy the example env file:

```bash
cp .env.example .env
```

2. Fill in at least:

```env
GEMINI_API_KEY=...
APP_DOMAIN=example.com
APP_BASE_URL=https://example.com
APP_SECRET_KEY=replace-this
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-this-too
```

3. Build and start the app:

```bash
docker compose build
docker compose up -d app
```

4. Check status:

```bash
docker compose ps
docker compose logs -f app
```

5. Open your site and sign in with the admin credentials from `.env`.

## Docker commands

### Start the web app

```bash
docker compose up -d app
```

### Stop the web app

```bash
docker compose down
```

### Run CLI commands through Docker

```bash
docker compose --profile tools run --rm cli info "https://www.pixiv.net/novel/show.php?id=27402134"
docker compose --profile tools run --rm cli translate "https://www.pixiv.net/novel/show.php?id=27402134"
```

### Run tests through Docker

```bash
docker compose --profile tools run --rm --entrypoint python cli -m unittest discover -s tests -v
```

## Environment variables

See `.env.example` for the full list.

Important ones:

- `GEMINI_API_KEY`: default Gemini API key
- `GEMINI_MODEL`: default model, currently `gemma-4-31b-it`
- `GEMINI_RPM_LIMIT`: per-key requests per minute, default `15`
- `GEMINI_RPD_LIMIT`: per-key requests per day, default `1500`
- `APP_DOMAIN`: domain used by Caddy labels in `compose.yaml`
- `APP_BASE_URL`: absolute base URL used in the app
- `APP_SECRET_KEY`: session signing key
- `ADMIN_USERNAME`: web admin login
- `ADMIN_PASSWORD`: web admin login password

## Gemini key behavior

- The `.env` key is the **default key**.
- Extra fallback keys can be added in the dashboard.
- Fallback keys are stored inside the Docker volume, not in git.
- Quota is tracked per key.
- If the default key is exhausted, the app falls back to the next available key.

## Notes

- Tracked files do **not** contain your real domain or secrets.
- Public Pixiv links only for now.
- Restricted/private Pixiv content is still out of scope.
