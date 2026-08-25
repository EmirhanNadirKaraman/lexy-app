# Lexy App

## Running locally

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — install and make sure it's running

### Steps

1. Clone this repo

2. Copy the example env file:
   ```
   cp .env.example .env
   ```

3. Open `.env` and set your `ANTHROPIC_API_KEY`
   (get one at https://console.anthropic.com — free tier works)

4. Start the app:
   ```
   cd lexy-app
   docker compose up
   ```

5. Open http://localhost:8000 in your browser

The first run takes a few minutes to download and build everything. Subsequent runs start in seconds.

### ⚠️ Step 4 does not currently succeed on a clean clone

**Verified 2026-08-25.** `docker compose up` starts Postgres with an empty
`postgres_data` volume, and `lexy-app/backend/entrypoint.sh` then runs
`alembic upgrade head` under `set -e` before starting the server. That upgrade
**fails at migration `006_video_channel_genre`**, which runs

```sql
ALTER TABLE video ADD COLUMN IF NOT EXISTS channel_id TEXT
```

against a `video` table **no migration ever creates**. `IF NOT EXISTS` there
applies to the *column*, not the table, so Postgres raises
`UndefinedTable: relation "video" does not exist`; `set -e` then kills the
container before `uvicorn` is reached.

The cause is that Alembic never owned the whole schema: migration `001` is
*initial_user_tables* and starts at the user/auth layer, on top of a content
schema (`video`, `sentence`, `word_table`, `phrase_blueprint`,
`language_table`, and others — 18 tables in total) that predates it and is
created by no file in this repository. `docs/SCHEMA.md` lists them.

**What this means for you:** these steps work against a database that already
has the content tables, which is how the project is actually run today. They do
not bootstrap a new one. Standing this project up from nothing needs a schema
dump or a baseline migration for those 18 tables, and neither exists in the
repo yet — that is real, tracked work, not a configuration mistake on your end.

This README previously stopped at step 5 and claimed the app would be running.

### Stopping

- `Ctrl+C` to stop
- `docker compose down` to clean up containers

To wipe all saved data: `docker compose down -v`
