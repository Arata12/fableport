# Fableport

Docker-first Pixiv novel and series translator with a built-in web dashboard, public reader, user accounts, queued jobs, Pixiv login fallback, and English exports.

## What it does

Fableport can:

- import **public Pixiv novels and series**
- retry **login-required Pixiv works** with optional Pixiv refresh tokens
- translate to **English** with Google AI Studio
- preserve **canonical Markdown** plus export **MD / TXT / HTML / EPUB**
- serve **public reader links** for finished works
- manage imports through a **web UI** or **CLI**
- queue web translations through a **single global worker**
- support **multiple users** with admin-managed accounts
- use **personal**, **global**, and **system** Gemini keys with quota tracking
- handle **embedded Pixiv novel images** in reader/export output
- **retranslate** an already imported work from the UI

Current default model: `gemma-4-31b-it`

## Main product features

- **Web dashboard** for imports, jobs, library, settings, and user management
- **Public reader** for translated works
- **CLI** for info/translate workflows
- **User accounts** stored persistently in the app volume
- **Admin-created users**
- **Personal Gemini keys** per user
- **Global fallback Gemini keys** for admins
- **Pixiv refresh token fallback**:
  - personal token
  - global token
  - system `PIXIV_REFRESH_TOKEN`
- **In-app Pixiv login flow** from Settings
- **Checkpointing and resume support**
- **Retranslate button** for improving existing imports with newer prompts/settings
- **Docker + Caddy labels** in `compose.yaml`

## Project docs

- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- [docs/USAGE.md](docs/USAGE.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Quick start

1. Copy the example env file:

```bash
cp .env.example .env
```

2. Set at least:

```env
GEMINI_API_KEY=your-google-ai-studio-key
APP_DOMAIN=example.com
APP_BASE_URL=https://example.com
APP_SECRET_KEY=replace-this-with-a-long-random-secret
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-this-too
```

3. Build and start the app:

```bash
docker compose up -d --build app
```

4. Open the site and sign in with the admin credentials from `.env`.

5. Add extra keys/tokens from **Settings** if needed.

## Docker usage

### Start or update the web app

```bash
docker compose up -d --build app
```

### Stop the app

```bash
docker compose down
```

### Check logs

```bash
docker compose logs -f app
```

### Run CLI commands

```bash
docker compose --profile tools run --rm cli info "https://www.pixiv.net/novel/show.php?id=27402134"
docker compose --profile tools run --rm cli translate "https://www.pixiv.net/novel/show.php?id=27402134"
docker compose --profile tools run --rm cli translate "https://www.pixiv.net/novel/series/11824916"
```

### Run tests

```bash
docker compose --profile tools run --rm --entrypoint python cli -m unittest discover -s tests -p 'test_*.py'
```

## Web workflow

### Dashboard tabs

- **Overview**: submit imports, view quota summary, recent jobs
- **Library**:
  - **My imports**: works you own/imported
  - **Public library**: works with public reader links
- **Settings**: password change, Gemini keys, Pixiv tokens, user management

### Import flow

1. Paste a Pixiv novel or series URL
2. Choose export formats
3. Submit the job
4. Wait for the queue to process it
5. Open the public reader or download exports

### Retranslate flow

Existing works can be retranslated from:

- the **Library** actions column
- the work **Admin** page

This queues a fresh translation job for the same Pixiv source URL and reuses the work’s output location/public link.

## Pixiv support

Supported source types:

- public Pixiv novels
- public Pixiv series
- login-required Pixiv novels/series if a refresh token is available

Fableport always tries **public fetch first**.

If Pixiv requires login, token resolution order is:

1. personal Pixiv token
2. global Pixiv token
3. `PIXIV_REFRESH_TOKEN` from `.env`

## Pixiv login / refresh tokens

### Recommended: in-app flow

In **Settings**:

1. click **Open Pixiv login**
2. sign into Pixiv in the new tab
3. copy the final callback URL or just the `code`
4. paste it back into Fableport

### Alternative: helper script

Local:

```bash
python scripts/pixiv_refresh_token.py login
```

Docker:

```bash
docker compose --profile tools run --rm --entrypoint python cli /app/scripts/pixiv_refresh_token.py login
```

By default the helper masks secrets on stdout. Use `--show-secrets` only if you explicitly need the raw token values.

## Gemini key behavior

Gemini key resolution order:

1. personal user keys
2. global admin fallback keys
3. system `.env` key

Notes:

- quota is tracked per key
- default limits are:
  - `15` requests/minute
  - `1500` requests/day
- if one key is exhausted, Fableport falls back to the next available key

## Storage model

Fableport stores data in the Docker volume mounted at `/app/output`.

Per imported work it keeps:

- metadata
- checkpoint state
- chapter markdown
- translated outputs
- localized embedded Pixiv image assets

Canonical stored text format is **Markdown**.

## Environment variables

See `.env.example` for the full list.

Important variables:

- `GEMINI_API_KEY` — system Gemini key
- `GEMINI_MODEL` — default translation model
- `GEMINI_RPM_LIMIT` — per-key requests/minute
- `GEMINI_RPD_LIMIT` — per-key requests/day
- `PIXIV_REFRESH_TOKEN` — optional system Pixiv fallback token
- `FANFICTL_OUTPUT_DIR` — output root
- `APP_DOMAIN` — Caddy label domain
- `APP_BASE_URL` — public absolute base URL
- `APP_SECRET_KEY` — session secret
- `ADMIN_USERNAME` — bootstrap admin username
- `ADMIN_PASSWORD` — bootstrap admin password
- `HOST` / `PORT` — app bind settings

## Compose notes

`compose.yaml` includes:

- `app` service for the web UI
- `cli` service profile for manual tools/commands
- Caddy labels for reverse proxying
- persistent `fanfictl_data` volume
- external `dockge_default` network

## Security / operational notes

- Do **not** commit `.env`
- Treat Pixiv refresh tokens and Gemini keys like secrets
- Use a strong `APP_SECRET_KEY`
- Change the default admin credentials before exposing the app publicly
- Public reader links are intended to be shareable

## License

This project is licensed under **AGPL-3.0-or-later**. See [LICENSE](LICENSE).
