# Deployment

## Requirements

- Docker
- Docker Compose
- Existing Caddy with `caddy-docker-proxy`
- External Docker network: `dockge_default`

## Compose layout

Services:

- `app`: FastAPI web app
- `cli`: optional tools profile for one-off commands

Persistent data:

- Docker named volume: `fanfictl_data`

The app stores all runtime data there:

- translated Markdown
- generated exports
- job records
- quota state
- fallback API keys

## Required `.env`

```env
GEMINI_API_KEY=...
GEMINI_MODEL=gemma-4-31b-it
GEMINI_RPM_LIMIT=15
GEMINI_RPD_LIMIT=1500

APP_DOMAIN=example.com
APP_BASE_URL=https://example.com
APP_SECRET_KEY=replace-this

ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-this

HOST=0.0.0.0
PORT=8000
```

## Start

```bash
docker compose build
docker compose up -d app
```

## Verify

```bash
docker compose ps
docker compose logs -f app
```

Expected result:

- `app` is `Up`
- Caddy sees labels from `compose.yaml`
- `/login` loads successfully

## Update

```bash
git pull
docker compose build
docker compose up -d app
```

## Stop

```bash
docker compose down
```

## Data location

List the named volume:

```bash
docker volume ls
```

The app does not rely on bind-mounted local output directories anymore.

## Security notes

- Keep all real values in `.env`
- Do not use the default `APP_SECRET_KEY`
- Use a non-trivial `ADMIN_PASSWORD`
- Extra fallback keys added in the dashboard are stored in the volume, not in `.env`
