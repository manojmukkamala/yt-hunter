# yt-hunter

**yt-hunter** extracts YouTube video transcripts, summarizes them using an LLM, stores them in SQLite, and shares the summary via Signal. The entire application lives in a single file: `main.py`.

The `CHANNELS` env var is a comma-separated list of YouTube channel handles (e.g., `SAMTIME,TheCarbonLayer`). Each handle is expanded at runtime into `https://www.youtube.com/@{handle}/videos`. The default is `TheCarbonLayer`. On first run for a given channel, all video entries are bulk-inserted as stubs (channel_id, channel_name, video id, title only). On subsequent runs, only videos not yet in the DB have their metadata, transcripts, and LLM summaries extracted. All rows are stored in `yt-hunter.db`.

## Setup

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

The `.env` file is git-ignored — never commit it. See `.env.example` for all available variables.

## Quick Start

```bash
# Install dependencies (uv managed, Python >= 3.13)
uv sync

# Run with one channel (default: TheCarbonLayer)
uv run python main.py

# Run with multiple channels
CHANNELS=SAMTIME,TheCarbonLayer uv run python main.py

# Run with Ollama (or any OpenAI-compatible provider)
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=anything  # Ollama does not require a real key
export OPENAI_MODEL=qwen2.5     # or whichever model you have pulled
uv run python main.py

# Run with Signal notifications (optional)
export SIGNAL_SERVER=http://signal-cli-REST-API-host:8080
export SIGNAL_SERVER_NUMBER=+1234567890  # the Signal server's registered number
export SIGNAL_RECEPIENT=+0987654321      # recipient number to receive summaries
uv run python main.py
```

## ⚠️ YouTube Transcript API Rate Limits

Calling the transcript API aggressively can get your IP **temporarily blocked by YouTube**. A scheduled run once per day is safe. This is why the first run for a new channel only inserts stubs (video id + title) and leaves transcripts and summaries `NULL` — bulk-pulling transcripts for every existing video would risk hitting those limits.

Going forward, each run processes only **new** videos since the last run, so transcript fetching stays light. Things to watch out for:

- A channel with several videos published between runs may trigger rate limits in a single pass.
- Being subscribed to many channels multiplies the number of new-video fetches per run.

If you have a lot of channels or channels that publish frequently, space out your runs (e.g., every few hours instead of once) or stick with daily and accept that some older stubs remain without transcripts.

## Docker

The project includes a `Dockerfile` and a `docker-compose.yml` that bundles the app alongside Ollama (LLM) and signal-cli-rest-api (notifications).

### Run with docker compose

```bash
# 1. Configure your .env
cp .env.example .env
vim .env   # fill in API keys, phone numbers, channels, etc.

# 2. Start all services (Ollama + Signal REST API)
docker compose up -d

# 3. Register Signal — scan the QR code with your phone open 
http://localhost:8080/v1/qrcodelink?device_name=yt-hunter

# 4. Run the app
docker compose run --rm yt-hunter
```

The `data/` directory is mounted into the container so the SQLite database persists across runs. Ollama and Signal state are stored in Docker named volumes (`ollama-data`, `signal-cli-data`).

### Use the published image

Pre-built images are available on GHCR after each release tag:

```bash
docker pull ghcr.io/manojmukkamala/yt-hunter:latest
```

When using the published image directly (without compose), ensure Ollama and Signal REST API are running separately, then set `OPENAI_BASE_URL` and `SIGNAL_SERVER` accordingly. Mount a local directory for the database:

```bash
docker run --rm -it \
  -v $(pwd)/data:/app/data \
  -e OPENAI_BASE_URL=http://host.docker.internal:11434/v1 \
  -e OPENAI_MODEL=gemma4:12b \
  -e CHANNELS=TheCarbonLayer \
  ghcr.io/manojmukkamala/yt-hunter:latest
```

### Environment Variables for Docker

| Variable | Example | Notes |
|---|---|---|
| `DB_PATH` | `data/yt-hunter.db` | SQLite database path inside the container |
| `OPENAI_BASE_URL` | `http://ollama:11434/v1` | Use `ollama` hostname in compose, `host.docker.internal` externally |
| `OPENAI_MODEL` | `gemma4:12b` | Model name (must be pulled/available in Ollama) |
| `OPENAI_API_KEY` | `` | Not required for Ollama; leave empty or set to `unused` |
| `CHANNELS` | `TheCarbonLayer` | Comma-separated YouTube handles |
| `SIGNAL_SERVER` | `http://signal-cli-rest-api:8080` | Use `signal-cli-rest-api` hostname in compose |
| `SIGNAL_SERVER_NUMBER` | `+1234567890` | Server's registered Signal number |
| `SIGNAL_RECEPIENT` | `+0987654321` | Recipient for summaries |

## Tooling

| Task | Command |
|---|---|
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Type-check | `uv run mypy .` |

Ruff is configured with an extensive rule set (flake8-bugbear, bandit, perflint, pylint minus design rules). Mypy runs in strict mode (`disallow_untyped_defs`, `warn_return_any`, etc.). The untyped imports (`yt_dlp`, `openai`, `pysignalclirestapi`) are silenced with `type: ignore[import-untyped]` comments so mypy passes cleanly.

## Architecture

```
┌──────────────┐     ┌───────────┐     ┌──────────────────┐
│  YouTube     │────▶│  yt-dlp   │────▶│ Channel Info     │
│  Channels    │     │ (metadata)│     │ (flat extract)   │
└──────────────┘     └───────────┘     └────────┬─────────┘
                                                │
                                  ┌─────────────▼───────────────┐
                                  │        SQLite DB            │
                                  │    (data/yt-hunter.db)      │
                                  │                             │
                                  │  video_transcripts table:   │
                                  │  id, channel_id/name,       │
                                  │  title, duration, views,    │
                                  │  url, timestamp,            │
                                  │  transcript, llm_summary    │
                                  └──┬─────────┬────────────────┘
                                     │         │
                    ┌────────────────┘         |
                    ▼                          ▼
          ┌─────────────────┐      ┌─────────────────────────────┐
          │  NEW CHANNEL    │      │     EXISTING CHANNEL        │
          │  (first run)    │      │     (incremental)           │
          ├─────────────────┤      ├─────────────────────────────┤
          │ Compare         │      │ Compare video_ids vs DB     │
          │ channel_id in   │      │ for this channel            │
          │ DB? → NOT FOUND │      │ → skip known videos         │
          ├─────────────────┤      ├─────────────────────────────┤
          │ Bulk-insert     │      │ For each NEW video:         │
          │ stubs (id,      │      │                             │
          │ title only)     │      │  ┌───────────┐              │
          │ Leave other     │      │  │ yt-dlp    │              │
          │ fields NULL     │      │  └─────┬─────┘              │
          │ for later fill  │      │        ▼ ── details         │
          │                 │      │  ┌───────────────┐          │
          │                 │      │  │ youtube-      │          │
          │                 │      │  │ transcript-   │          │
          │                 │      │  │ api           │          │
          │                 │      │  └──────┬────────┘          │
          │                 │      │         ▼ ── transcript     │
          │                 │      │  ┌───────────────┐          │
          │                 │      │  │ LLM (OpenAI-  │          │
          │                 │      │  │ compatible)   │          │
          │                 │      │  └──────┬────────┘          │
          │                 │      │         ▼ ── llm_summary    │
          │                 │      │  ┌───────────────┐          │
          │                 │      │  │ Insert full   │          │
          │                 │      │  │ row into DB   │          │
          │                 │      │  └──────┬────────┘          │
          │                 │      │         ▼ ── llm_summary    │
          │                 │      │  ┌───────────────┐          │
          │                 │      │  │ Signal CLI    │(optional)│
          │                 │      │  │ REST API      │          │
          │                 │      │  └───────────────┘          │
          └─────────────────┘      └─────────────────────────────┘
```

**Pipeline:** iterate each channel URL → fetch channel info via yt-dlp (flat) → check if channel exists in DB. **New channel**: extract channel_id, channel_name, and all entry stubs (video id, title), bulk-insert into DB with other fields left null. **Existing channel**: compare entry video ids against DB for that channel; for each new video, fetch details → transcript → LLM summary → insert full row → send Signal message (if configured). Dedup is per-channel (keyed on `channel_id` + video `id`).

Key functions in `main.py`:
- `db_init()` — creates the `video_transcripts` table if it does not exist
- `db_get_existing_channel_ids()` — returns distinct channel_ids already stored
- `db_get_video_ids_for_channel(channel_id)` — returns video ids for a given channel
- `db_bulk_insert_stubs(conn, channel_id, channel_name, entries)` — bulk-inserts entry stubs (`INSERT OR IGNORE`, only sets channel_id, channel_name, id, title)
- `db_insert()` — inserts a full video row (`INSERT OR IGNORE`, keyed on `id`)
- `fetch_channel_info()` — fetches full channel info object via yt-dlp (flat); raises ValueError if the channel is inaccessible
- `check_and_extract()` — orchestrates the pipeline; handles new-channel stub insertion and existing-channel incremental processing, including Signal notifications per video when a `signal_client` and `signal_recepient` are provided
- `get_video_details()` — fetches metadata for a single video by ID
- `get_transcript()` — joins caption snippets into a single string via `YouTubeTranscriptApi`
- `summarize_transcript()` — sends transcript to an LLM via the OpenAI-compatible API; returns a concise summary. All env var reads happen in `__main__`, not inside functions, for deterministic behavior and testability.

**Database:** SQLite (`data/yt-hunter.db`) with a single `video_transcripts` table:
| Column | Type | Notes |
|---|---|---|
| load_time | TIMESTAMP | defaults to CURRENT_TIMESTAMP |
| id | TEXT | primary key (YouTube video id) |
| channel_id | TEXT | nullable — UC-style YouTube channel ID |
| channel_name | TEXT | nullable |
| title | TEXT | nullable |
| duration | REAL | nullable |
| view_count | INTEGER | nullable |
| url | TEXT | nullable |
| timestamp | INTEGER | nullable (video publish epoch) |
| transcript | TEXT | nullable |
| llm_summary | TEXT | nullable |

**Dependencies:**
- `yt-dlp` — video/channel metadata extraction
- `youtube-transcript-api` — caption fetching
- `openai` — LLM summarization via OpenAI-compatible API (works with Ollama, any compatible endpoint)
- `pysignalclirestapi` — Signal notifications via the signal-cli REST API

**Design note:** All environment variable reads (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `SIGNAL_SERVER`, `SIGNAL_SERVER_NUMBER`, `SIGNAL_RECEPIENT`) happen in the `if __name__ == '__main__'` block. Functions take explicit parameters only, keeping them deterministic and easy to test.
