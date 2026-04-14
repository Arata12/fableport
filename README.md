# Fableport

Docker-first Pixiv fanfic translator and reader.

It can:
- fetch public Pixiv novels and series
- retry login-required Pixiv works with optional authenticated Pixiv access
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
- **Single global translation queue** for the web app
- **Gemini quota tracking**
- **Personal and global fallback API keys** stored in the app volume
- **User accounts** with admin-managed creation
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
- `PIXIV_REFRESH_TOKEN`: optional system Pixiv refresh token used as the last authenticated fallback
- `APP_DOMAIN`: domain used by Caddy labels in `compose.yaml`
- `APP_BASE_URL`: absolute base URL used in the app
- `APP_SECRET_KEY`: session signing key
- `ADMIN_USERNAME`: web admin login
- `ADMIN_PASSWORD`: web admin login password

## Gemini key behavior

- The `.env` key is the **system key**.
- Users can add **personal keys** in Settings.
- Admins can add **global fallback keys**.
- Keys are stored inside the Docker volume, not in git.
- Quota is tracked per key.
- Key resolution order is: personal keys -> global fallback keys -> system key.

## Pixiv token behavior

- Public Pixiv fetch is always tried first.
- If a Pixiv work requires login, Fableport retries with Pixiv refresh tokens.
- Resolution order is: personal Pixiv token -> global Pixiv token -> system `.env` Pixiv token.
- Admins can add global Pixiv refresh tokens in `Settings`.
- Users can add personal Pixiv refresh tokens in `Settings`.

## How to get a Pixiv refresh token

Recommended method:

You can do this directly inside **Settings** now:

1. open `Settings`
2. in the Pixiv token section, click **Open Pixiv login**
3. sign into Pixiv
4. copy the callback URL or the `code`
5. paste it back into Fableport

Or use the bundled helper:

1. run locally:

```bash
python scripts/pixiv_refresh_token.py login
```

Or with Docker:

```bash
docker compose --profile tools run --rm --entrypoint python cli /app/scripts/pixiv_refresh_token.py login
```

2. a browser window will open to Pixiv login
3. sign in normally
4. after the callback step, paste the full callback URL or the `code` into the helper prompt
5. the helper prints the `refresh_token`
6. paste that token into Fableport Settings, or store it as `PIXIV_REFRESH_TOKEN` in `.env`

Notes:

- the refresh token is what Fableport needs
- do **not** paste your Pixiv password into the app
- treat the refresh token like a secret
- the system `.env` token is used as the final authenticated fallback

## License

This project is licensed under **AGPL-3.0-or-later**. See [LICENSE](LICENSE).

## Notes

- Tracked files do **not** contain your real domain or secrets.
- Public Pixiv fetch is the default path.
- Login-required Pixiv works can be retried with configured Pixiv refresh tokens.
