import logging
import os
import sqlite3
from typing import Any, cast

import openai  # type: ignore[import-untyped]
import yt_dlp  # type: ignore[import-untyped]
from pysignalclirestapi import SignalCliRestApi  # type: ignore[import-untyped]
from youtube_transcript_api import YouTubeTranscriptApi, YouTubeTranscriptApiException


def db_init(conn: sqlite3.Connection) -> None:
    """Create the videos table if it does not exist."""
    conn.execute(
        """\
CREATE TABLE IF NOT EXISTS video_transcripts (
    load_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    id TEXT PRIMARY KEY NOT NULL,
    channel_id TEXT,
    channel_name TEXT,
    title TEXT,
    duration REAL,
    view_count INTEGER,
    url TEXT,
    timestamp INTEGER,
    transcript TEXT,
    llm_summary TEXT
)"""
    )


def db_get_existing_channel_ids(conn: sqlite3.Connection) -> set[str]:
    """Return the set of channel_ids already stored in the table."""
    cur = conn.execute(
        'SELECT DISTINCT channel_id FROM video_transcripts WHERE channel_id IS NOT NULL'
    )
    return {row[0] for row in cur}


def db_get_video_ids_for_channel(conn: sqlite3.Connection, channel_id: str) -> set[str]:
    """Return the set of video ids already stored for a given channel."""
    cur = conn.execute(
        'SELECT id FROM video_transcripts WHERE channel_id = ?', (channel_id,)
    )
    return {row[0] for row in cur}


def db_bulk_insert_stubs(
    conn: sqlite3.Connection,
    channel_id: str,
    channel_name: str,
    entries: list[dict[str, Any]],
) -> int:
    """Bulk-insert video stubs (channel_id, channel_name, id, title only).

    Returns the number of rows inserted.
    """
    data = [
        (
            channel_id,
            channel_name,
            entry['id'],
            entry.get('title'),
        )
        for entry in entries
    ]
    cur = conn.executemany(
        """\
INSERT OR IGNORE INTO video_transcripts (channel_id, channel_name, id, title)
VALUES (?, ?, ?, ?)""",
        data,
    )
    return cur.rowcount


def db_insert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert a new video record. Skips if the id already exists."""
    conn.execute(
        """\
INSERT OR IGNORE INTO video_transcripts (
    id, channel_id, channel_name, title, duration, view_count, url, timestamp, transcript, llm_summary
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row['id'],
            row.get('channel_id'),
            row.get('channel_name'),
            row.get('title'),
            row.get('duration'),
            row.get('view_count'),
            row.get('url'),
            row.get('timestamp'),
            row.get('transcript'),
            row.get('llm_summary'),
        ),
    )


def fetch_channel_info(channel_url: str, ydl_opts: dict[str, Any]) -> dict[str, Any]:
    """Fetch full channel info via yt-dlp. Raises ValueError if not found or inaccessible."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(channel_url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            msg = f'Channel not found or inaccessible: {channel_url}'
            raise ValueError(msg) from exc

        return cast('dict[str, Any]', info)


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
        logger.info('Generating summary for transcript for %s', title)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    'role': 'user',
                    'content': (
                        f'You are given the transcript of a YouTube video titled "{title}".\n'
                        'Summarize the transcript covering the key points and ingore any promotions and endorsements by the author.'
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
    channel_urls: list[str],
    ydl_opts: dict[str, Any] | None = None,
    ytt_api: YouTubeTranscriptApi | None = None,
    llm_client: openai.OpenAI | None = None,
    model: str = 'gemma4:12b',
    signal_client: SignalCliRestApi | None = None,
    signal_recepient: str | None = None,
) -> tuple[int, int]:
    """Return (stubs_inserted, full_pipeline_inserted)."""
    opts: dict[str, Any] = ydl_opts or {}

    known_channel_ids = db_get_existing_channel_ids(conn)
    conn.commit()

    stubs_inserted = 0
    full_inserted = 0

    for channel_url in channel_urls:
        info = fetch_channel_info(channel_url, opts)

        channel_id = cast('str', info.get('channel_id') or info.get('uploader_id'))
        channel_name = cast(
            'str',
            info.get('channel') or info.get('uploader') or info.get('title'),
        )
        if not channel_id and not info.get('entries'):
            logger.error('No valid channel data for %s', channel_url)
            continue

        entries = info.get('entries', [])
        if not entries:
            logger.error('No videos found for %s (%s)', channel_name, channel_url)
            continue

        if channel_id not in known_channel_ids:
            # New channel: bulk-insert stubs (channel_id, channel_name, id, title only).
            inserted = db_bulk_insert_stubs(
                conn, cast('str', channel_id), channel_name, entries
            )
            logger.info(
                'New channel "%s" (%s): %d video stub(s) inserted for %d total.',
                channel_name,
                channel_url,
                inserted,
                len(entries),
            )
            conn.commit()
            known_channel_ids.add(channel_id)
            stubs_inserted += inserted
            continue

        # Existing channel: process only videos that are not yet in the DB.
        existing_video_ids = db_get_video_ids_for_channel(conn, cast('str', channel_id))
        new_entries = [e for e in entries if e['id'] not in existing_video_ids]
        logger.info(
            'Channel "%s": %d new video(s) to process out of %d total.',
            channel_name,
            len(new_entries),
            len(entries),
        )

        for entry in new_entries:
            details = get_video_details(entry['id'], opts)

            transcript = None
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
                'channel_id': channel_id,
                'channel_name': channel_name,
                'title': details.get('title'),
                'duration': details.get('duration'),
                'view_count': details.get('view_count'),
                'url': f'https://www.youtube.com/watch?v={entry["id"]}',
                'timestamp': details.get('timestamp'),
                'transcript': transcript,
                'llm_summary': llm_summary,
            }
            db_insert(conn, row)
            logger.info(
                'Inserted transcript for "%s" (id=%s)',
                row['title'],
                row['id'],
            )
            full_inserted += 1

            if signal_client and signal_recepient:
                video_title = details.get('title') or row['title'] or ''
                message = (
                    "Here is the summary of '"
                    + video_title
                    + "' posted by "
                    + channel_name
                    + '  \n'
                )
                if llm_summary:
                    message += llm_summary
                message += '\n'
                signal_client.send_message(message=message, recipients=signal_recepient)

    conn.commit()
    return stubs_inserted, full_inserted


if __name__ == '__main__':
    from dotenv import load_dotenv

    load_dotenv()  # reads .env file if present; no-op otherwise

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    logger = logging.getLogger(__name__)

    db_path: str = os.getenv('DB_PATH', 'data/yt-hunter.db')
    conn = sqlite3.connect(db_path)
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

    channels_raw: str = os.getenv('CHANNELS', 'TheCarbonLayer')
    channel_names = [name.strip() for name in channels_raw.split(',') if name.strip()]
    channel_urls = [f'https://www.youtube.com/@{name}/videos' for name in channel_names]

    SIGNAL_SERVER = os.getenv('SIGNAL_SERVER')
    SIGNAL_SERVER_NUMBER = os.getenv('SIGNAL_SERVER_NUMBER')
    SIGNAL_RECEPIENT = os.getenv('SIGNAL_RECEPIENT')

    signal_client = SignalCliRestApi(SIGNAL_SERVER, SIGNAL_SERVER_NUMBER)

    stubs_inserted, full_inserted = check_and_extract(
        logger,
        conn,
        channel_urls=channel_urls,
        ydl_opts=ydl_opts,
        ytt_api=ytt_api,
        llm_client=llm_client,
        model=model,
        signal_client=signal_client,
        signal_recepient=SIGNAL_RECEPIENT,
    )
    conn.close()

    logger.info(
        'Done. %d stub(s) inserted, %d full transcript(s) sourced.',
        stubs_inserted,
        full_inserted,
    )
