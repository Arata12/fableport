# Usage

## Web UI

### Login

- Open `/login`
- Sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`
- Admin can create more users in `Settings`

### Submit a Pixiv work

From the dashboard:

1. paste a public Pixiv novel or series URL
2. optionally set a chapter limit
3. optionally enable resume
4. choose export formats
5. start translation

## Job flow

Jobs move through these stages:

- fetching
- translating
- exporting
- completed
- failed

Only one web translation job runs at a time. Other submitted jobs wait in the global queue.

If the app restarts during a job, the job is marked interrupted/failed and can be submitted again with resume enabled.

## Reader

Completed works get a public reader link.

Reader behavior:

- translated English title
- public reading page
- public download links
- single line breaks preserved for prose/dialogue
- chapter navigation for series

## Exports

Supported formats:

- `md`
- `txt`
- `html`
- `epub`

## CLI through Docker

### Inspect a Pixiv work

```bash
docker compose --profile tools run --rm cli info "https://www.pixiv.net/novel/show.php?id=27402134"
```

### Translate a Pixiv work

```bash
docker compose --profile tools run --rm cli translate "https://www.pixiv.net/novel/show.php?id=27402134"
```

## Gemini quota panel

The dashboard shows:

- requests used this minute
- requests used today
- reset time
- last quota event

The totals reflect all active keys combined.

## Gemini keys

The dashboard supports personal and global Gemini keys.

Behavior:

- users can add personal keys for their own jobs
- admins can add global fallback keys
- `.env` key remains the system key
- extra keys are masked in the UI
- translation automatically falls back when another key is exhausted

## Pixiv tokens

The dashboard also supports Pixiv refresh tokens.

Behavior:

- public Pixiv fetch is tried first
- if the work requires login, Fableport retries with Pixiv auth
- users can add personal Pixiv refresh tokens in `Settings`
- admins can add global Pixiv refresh tokens in `Settings`
- `.env` can hold a system `PIXIV_REFRESH_TOKEN`
- fallback order is: personal token -> global token -> system token

### Getting a Pixiv refresh token

Use the in-app Settings flow:

1. go to `Settings`
2. click `Open Pixiv login`
3. sign into Pixiv
4. copy the callback URL or the `code`
5. paste it into the completion form in Fableport

You can also use the bundled helper:

```bash
python scripts/pixiv_refresh_token.py login
```

Or through Docker:

```bash
docker compose --profile tools run --rm --entrypoint python cli /app/scripts/pixiv_refresh_token.py login
```

Then:

1. sign into Pixiv in the opened browser
2. copy the callback URL or OAuth `code`
3. paste it into the helper prompt
4. copy the printed `refresh_token`
5. add it in `Settings`, or put it in `.env` as `PIXIV_REFRESH_TOKEN`

## Limits

Current defaults:

- `15` requests per minute per key
- `1500` requests per day per key

These can be changed with:

- `GEMINI_RPM_LIMIT`
- `GEMINI_RPD_LIMIT`
