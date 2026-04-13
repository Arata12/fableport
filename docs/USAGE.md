# Usage

## Web UI

### Login

- Open `/login`
- Sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`

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

## Fallback keys

The dashboard supports adding more Gemini keys.

Behavior:

- `.env` key remains primary/default
- extra keys are masked in the UI
- extra keys can be removed
- translation automatically falls back when another key is exhausted

## Limits

Current defaults:

- `15` requests per minute per key
- `1500` requests per day per key

These can be changed with:

- `GEMINI_RPM_LIMIT`
- `GEMINI_RPD_LIMIT`
