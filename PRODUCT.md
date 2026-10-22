# Awesome Atlas

Product summary for the whole repository. The web app has its own UX-focused
[apps/awesome-web/PRODUCT.md](apps/awesome-web/PRODUCT.md); this file states what exists today.

## Purpose

Browse GitHub "Awesome" lists in one place: see what each list contains, compare lists for
shared and unique projects, rank projects by how many lists mention them, and read the daily
GitHub Trending archive. Public data only, no login.

## Users

Developers choosing open-source tools. No user has been confirmed; there is no analytics.

## What works

- `services/awesome-api` (FastAPI, Python 3.12): a background worker fetches list READMEs from
  public raw GitHub URLs every 30 minutes, stores them in its own data lake (Parquet) and
  SQLite index on a volume, and serves the `awesome.v1` contract
  (`services/awesome-api/contracts/awesome.v1.md`): projects, sources, categories, comparison,
  mention ranking and the daily archive. No GitHub token is used.
- `apps/awesome-web` (React 19 + Vite, served by Caddy): Thai UI for search, categories,
  list comparison, ranking and saved projects (saved in the browser only).
- Tests: Python pytest suite (`services/awesome-api/tests`), Vitest unit test and Playwright
  browser specs (`apps/awesome-web/tests`). CI runs lint, mypy, pytest with a coverage gate,
  tsc, Vitest, the web build and the shared security scans.
- Error reporting: the API sends errors to Sentry when `SENTRY_DSN` is set (scrubbed, errors only;
  `services/awesome-api/awesome_api/_sentry.py`). The web is static files with no server process.

## How to run

See [README.md](README.md): start the API on port 3510, then `npm run dev` for the web on 3500.
API dependencies are locked in `services/awesome-api/uv.lock`; `requirements.txt` is exported
from it for the Docker image (`uv sync --frozen --all-extras` for local development).

## Live

<https://awesome-web-production.up.railway.app> (Railway project `awesome-atlas`, services
`awesome-web` and `awesome-api`, deployed from `main` after CI passes — "Wait for CI").
On 2026-10-22 the page answered HTTP 200 and `/api/v1/status` reported `state: degraded` with
1,481 of 1,944 sources synced; some list READMEs fail to fetch, so coverage is partial.

## Limits

- Coverage is partial and changes each sync; absence from the index does not mean a project is
  absent from every Awesome list, and mention counts are not a quality score.
- Single API instance with a local volume; no backups of the derived index (it can be rebuilt
  by re-syncing public sources).
- No accounts, no server-side saved state, no analytics, no confirmed users.
- Railway "Wait for CI" deploys are currently stuck at the owner level (see the batch report);
  the live site may lag `main`.

## Disposition

Portfolio showcase (product card verdict `showcase`, 2026-10-09): a working public data product
kept as evidence of a lake-first pipeline. No revenue plan.
