# Fableport

Docker-first Pixiv novel and series importer & translator with a built-in web dashboard, public reader, user accounts, queued jobs, Pixiv login fallback, local/vLLM/Ollama inference, managed Google Colab GPU sessions, and multi-format exports.

## What it does

Fableport can:

- import **public Pixiv novels and series** (single or batch URLs) **without translating first**
- retry **login-required Pixiv works** with optional Pixiv refresh tokens
- translate on demand (**Translate**, **Resume**, **Retranslate**, **Translate all pending**) into any **configurable target language** (`English`, `Spanish`, etc.)
- connect to **local / remote inference engines**:
  - **OpenAI-compatible (`/v1/chat/completions`)** — vLLM, local Ollama, llama.cpp, LM Studio, etc. (API key optional)
  - **Ollama native (`/api/chat`)** — Ollama Cloud or local Ollama
- **manage Google Colab GPU sessions directly from Docker** via `google-colab-cli` + in-container `ssh -L` port forwarding (auto-provisions L4/A100/T4/H100, starts vLLM or Ollama, translates your queue, and auto-stops when idle to save compute credits)
- preserve **canonical Markdown** plus export **MD / TXT / HTML / EPUB**
- serve **public reader links** for imported and translated works
- manage imports through a **web UI** or **CLI**
- run fast **Pixiv imports** independently from the **single global translation worker**
- support **multiple users** with admin-managed accounts
- use **personal**, **global**, and **system** API keys with usage tracking
- handle **embedded Pixiv novel images** in reader/export output

Current default model: `gemma-4-31b-it`

## Main product features

- **Web dashboard** for imports, jobs, library, settings, and user management
- **Public reader** for translated works
- **CLI** for info/translate workflows
- **User accounts** stored persistently in the app volume
- **Admin-created users**
- **Personal Ollama API keys** per user
- **Global fallback Ollama API keys** for admins
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
OLLAMA_API_KEY=your-ollama-cloud-key
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
docker compose --profile tools run --rm cli import "https://www.pixiv.net/novel/show.php?id=27402134"
docker compose --profile tools run --rm cli translate "https://www.pixiv.net/novel/show.php?id=27402134" --target Spanish
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
- **Settings**: password change, Ollama API keys, Pixiv tokens, user management

### Import & translate flow

1. Paste one or more Pixiv novel or series URLs (one per line) on **Overview**
2. Click **Import to library** (fast, no GPU or API key required) — or **Import & translate now**
3. Review imported works in **Library** (shown as `Untranslated`, `Partial`, or `Translated`)
4. Click **Translate** on individual works or **Translate all pending** to queue the whole batch
5. Open the public reader or download exports (`MD`, `TXT`, `HTML`, `EPUB`)

### Local inference (vLLM / Ollama / OpenAI-compatible)

In **Settings → Translation engine**:

- Choose **API mode**: `OpenAI-compatible (/v1/chat/completions)` (works with vLLM, local Ollama, llama.cpp, etc.) or `Ollama native (/api/chat)`.
- Set **Target language** (e.g. `English`, `Spanish`, etc.).
- Point **Host / Base URL** at your server (e.g. `http://host.docker.internal:11434` for Ollama on the Docker host or `http://host.docker.internal:8001` for vLLM). No API key is required for non-cloud hosts.
- Click **Test connection** to verify reachability and available models.

### Managed Google Colab GPU (Docker)

The Docker image bundles `google-colab-cli` and `openssh-client` so Fableport can launch and stop Colab GPUs itself:

1. Open **Settings → Google Colab GPU**
2. Click **Open Google Colab login**, sign into Google, and paste the authorization code back into Fableport (or paste an existing `application_default_credentials.json`)
3. Choose GPU (`L4`, `A100`, `T4`, `H100`), engine (`vLLM` recommended for parallel chapters, or `Ollama`), model, optional HuggingFace token (for gated models like Gemma), and **Auto-start** / **Auto-stop**
4. Enable **Use managed Google Colab GPU for translation jobs** and save
5. Import your fanfics first without spending GPU credits, then click **Translate all pending** — Fableport allocates the GPU, boots vLLM/Ollama, opens an internal `ssh -L` tunnel via `colab ssh --proxy-mode`, translates the queue with per-chunk checkpoints, and runs `colab stop` automatically when finished.

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

## API key behavior

API key resolution order:

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

- `LLM_API_MODE` — `ollama` (`/api/chat`) or `openai` (`/v1/chat/completions`, for vLLM, local Ollama, llama.cpp, etc.)
- `TARGET_LANGUAGE` — target translation language (default `English`, e.g. `Spanish`)
- `OLLAMA_API_KEY` — optional API key / Bearer token (required for Ollama Cloud)
- `OLLAMA_MODEL` — default translation model (`gemma4:31b`)
- `OLLAMA_HOST` — API base URL (default `https://ollama.com`, e.g. `http://host.docker.internal:11434`)
- `OLLAMA_RPM_LIMIT` / `OLLAMA_RPD_LIMIT` — optional per-key caps (`0` = none)
- `TRANSLATION_CONCURRENCY` — chapters translated in parallel (default `3`)
- `COLAB_ENABLED` / `COLAB_GPU` / `COLAB_ENGINE` / `COLAB_MODEL` / `COLAB_HF_TOKEN` / `COLAB_AUTO_START` / `COLAB_AUTO_STOP` — managed Google Colab GPU runtime settings
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
- Treat Pixiv refresh tokens and Ollama API keys like secrets
- Use a strong `APP_SECRET_KEY`
- Change the default admin credentials before exposing the app publicly
- Public reader links are intended to be shareable

## License

This project is licensed under **AGPL-3.0-or-later**. See [LICENSE](LICENSE).
