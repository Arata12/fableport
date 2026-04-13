# Fableport Architecture

## Overview

The project has one shared Python core used by both:

- the CLI
- the FastAPI web app

## Main modules

### `pixiv.py`

- parses Pixiv novel and series URLs
- calls public Pixiv AJAX endpoints
- normalizes payloads into internal work/chapter models

### `content.py`

- converts Pixiv text and tags into normalized Markdown
- handles markers like:
  - `[chapter:...]`
  - `[newpage]`
  - `[[rb:... > ...]]`
  - `[[jumpuri:... > ...]]`

### `translate.py`

- wraps Google AI Studio access
- translates titles, synopsis, and prose chunks
- now supports multiple runtime keys
- uses quota tracking and key fallback
- normalizes dialogue quotes for English output

### `workflow.py`

- main end-to-end translation pipeline
- fetch -> checkpoint -> translate -> export

### `quota.py`

- per-key quota tracking
- request-per-minute throttling
- request-per-day accounting
- fallback selection across available keys

### `jobs.py`

- persistent queued job records
- one global worker for web submissions
- FIFO queue processing
- queued jobs survive restarts

### `keystore.py`

- resolves personal keys, global fallback keys, and the system `.env` key
- stores non-env keys in runtime app data
- exposes masked summaries for the dashboard

### `webapp.py`

- login/logout
- dashboard tabs: overview, library, settings
- submit route
- job pages
- reader pages
- public downloads
- fallback-key management
- password change
- admin user creation

## Persistence

Stored in the Docker volume:

- works and chapter files
- exports
- job metadata
- quota metadata
- fallback API keys

## Output layout

The app stores works by Pixiv id:

- `novel-<pixiv_id>/`
- `series-<pixiv_id>/`

Typical contents:

- `metadata.json`
- `checkpoint.json`
- `translated.md` or `combined.md`
- `translated.txt` or `combined.txt`
- `translated.html` or `combined.html`
- `translated.epub` or `combined.epub`
- `chapters/`

## Reader rendering

- canonical source is Markdown
- browser output is rendered HTML
- HTML is sanitized before display
- single line breaks are preserved for better fiction reading

## Web auth

Auth uses stored users in SQLite with a bootstrap admin from `.env`.
