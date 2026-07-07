import logging
import os
import sqlite3
from typing import Any

import openai  # type: ignore[import-untyped]
import yt_dlp  # type: ignore[import-untyped]
from youtube_transcript_api import YouTubeTranscriptApi, YouTubeTranscriptApiException


def db_init(conn: sqlite3.Connection) -> None:
    """Create the videos table if it does not exist."""
    conn.execute(
        """\
CREATE TABLE IF NOT EXISTS video_transcripts (
    load_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    id TEXT PRIMARY KEY NOT NULL,
    title TEXT,
    duration REAL,
    view_count INTEGER,
    url TEXT,
    timestamp INTEGER,
    transcript TEXT,
    llm_summary TEXT
)"""
    )


def db_get_existing_ids(conn: sqlite3.Connection) -> set[str]:
    """Return the set of video ids already stored in the table."""
    cur = conn.execute('SELECT id FROM video_transcripts')
    return {row[0] for row in cur}


def db_insert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert a new video record. Skips if the id already exists."""
    conn.execute(
        """\
INSERT OR IGNORE INTO video_transcripts (id, title, duration, view_count, url, timestamp, transcript, llm_summary)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row['id'],
            row.get('title'),
            row.get('duration'),
            row.get('view_count'),
            row.get('url'),
            row.get('timestamp'),
            row.get('transcript'),
            row.get('llm_summary'),
        ),
    )


def get_latest_video(
    channel_url: str, ydl_opts: dict[str, Any]
) -> list[dict[str, Any]]:
    """Verify the channel is valid and fetches the info. Raises ValueError if not found or inaccessible."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(channel_url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            msg = f'Channel not found or inaccessible: {channel_url}'
            raise ValueError(msg) from exc

        channel_name = info.get('channel') or info.get('uploader') or info.get('title')
        if not channel_name and not info.get('entries'):
            msg = f'No valid channel data for URL: {channel_url}'
            raise ValueError(msg)

        entries: list[dict[str, Any]] = info.get('entries', [])
        return entries


def get_video_details(video_id: str, ydl_opts: dict[str, Any]) -> dict[str, Any]:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        details: dict[str, Any] = ydl.extract_info(
            f'https://www.youtube.com/watch?v={video_id}', download=False
        )
        return details


def get_transcript(
    logger: logging.Logger, video_id: str, ytt_api: YouTubeTranscriptApi
) -> str | None:
    try:
        transcript = ytt_api.fetch(video_id)
        return ' '.join(snippet.text for snippet in transcript)
    except YouTubeTranscriptApiException:
        logger.exception('No transcript available')
        return None


def summarize_transcript(
    logger: logging.Logger,
    client: openai.OpenAI,
    transcript: str | None,
    title: str,
    model: str,
) -> str | None:
    """Return a concise LLM summary of the transcript.

    Uses the OpenAI-compatible API (works with Ollama via OPENAI_BASE_URL).
    Returns None if no transcript is available or summarization fails.
    """
    if not transcript:
        return None

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    'role': 'user',
                    'content': (
                        f'You are given the transcript of a YouTube video titled "{title}".\n'
                        'Summarize it in 2-3 concise sentences covering the key points.'
                    ),
                },
                {'role': 'user', 'content': transcript},
            ],
        )
        content = response.choices[0].message.content if response.choices else None
        return content.strip() if content else None
    except Exception:
        logger.exception('Summarization failed')
        return None


def check_and_extract(
    logger: logging.Logger,
    conn: sqlite3.Connection,
    channel_url: str,
    ydl_opts: dict[str, Any] | None = None,
    ytt_api: YouTubeTranscriptApi | None = None,
    llm_client: openai.OpenAI | None = None,
    model: str = 'gemma4:12b',
) -> int:
    opts: dict[str, Any] = ydl_opts or {}
    entries = get_latest_video(channel_url, opts)
    if not entries:
        logger.error('No videos found.')
        return 0

    existing_ids = db_get_existing_ids(conn)
    conn.commit()

    inserted = 0
    for entry in entries:
        if entry['id'] in existing_ids:
            continue

        details = get_video_details(entry['id'], opts)
        if not ytt_api:
            msg = 'ytt_api is required for transcript extraction'
            raise ValueError(msg)
        transcript = get_transcript(logger, entry['id'], ytt_api)

        llm_summary: str | None = None
        if llm_client and transcript:
            llm_summary = summarize_transcript(
                logger,
                llm_client,
                transcript,
                title=details.get('title') or '',
                model=model,
            )

        row = {
            'id': entry['id'],
            'title': details.get('title'),
            'duration': details.get('duration'),
            'view_count': details.get('view_count'),
            'url': f'https://www.youtube.com/watch?v={entry["id"]}',
            'timestamp': details.get('timestamp'),
            'transcript': transcript,
            'llm_summary': llm_summary,
        }
        db_insert(conn, row)
        logger.info('Inserted video "%s" (id=%s)', row['title'], row['id'])
        inserted += 1

        # TODO #2: Send a message to mattermost with LLM Summary as the text. ref: https://developers.mattermost.com/api-documentation/#/operations/CreatePost

    conn.commit()
    return inserted


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    logger = logging.getLogger(__name__)

    conn = sqlite3.connect('yt-hunter.db')
    db_init(conn)

    ytt_api = YouTubeTranscriptApi()

    llm_client: openai.OpenAI | None = None
    model: str = os.getenv('OPENAI_MODEL', 'gemma4:12b') or 'gemma4:12b'
    if os.getenv('OPENAI_API_KEY') or os.getenv('OPENAI_BASE_URL'):
        llm_client = openai.OpenAI(
            api_key=os.getenv('OPENAI_API_KEY', 'not-needed'),
            base_url=os.getenv('OPENAI_BASE_URL'),
        )

    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }

    count = check_and_extract(
        logger,
        conn,
        channel_url='https://www.youtube.com/@SAMTIME/videos',
        ydl_opts=ydl_opts,
        ytt_api=ytt_api,
        llm_client=llm_client,
        model=model,
    )
    conn.close()

    logger.info('Done. %d transcripts(s) sourced.', count)
