import logging

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi, YouTubeTranscriptApiException


def get_latest_video(channel_url: str, ydl_opts: dict) -> list[dict]:
    """Verify the channel is valid and fetches the info. Raises ValueError if not found or inaccessible."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(channel_url, download=False)
        except yt_dlp.utils.DownloadError as e:
            msg = f"Channel not found or inaccessible: {channel_url}"
            raise ValueError(msg) from e

        channel_name = info.get("channel") or info.get("uploader") or info.get("title")
        if not channel_name and not info.get("entries"):
            msg = f"No valid channel data for URL: {channel_url}"
            raise ValueError(msg)

        entries = info.get("entries", [])
        return entries


def get_video_details(video_id: str, ydl_opts: dict) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )


def get_transcript(logger: logging.Logger, video_id: str, ytt_api: YouTubeTranscriptApi) -> str | None:
    try:
        transcript = ytt_api.fetch(video_id)
        return " ".join(snippet.text for snippet in transcript)
    except YouTubeTranscriptApiException:
        logger.exception("No transcript available")
        return None


def check_and_extract(
    logger: logging.Logger,
    channel_url: str,
    ydl_opts: dict | None = None,
    ytt_api: YouTubeTranscriptApi | None = None,
) -> tuple[int | None, str | None]:
    entries = get_latest_video(channel_url, ydl_opts)
    if not entries:
        logger.error("No videos found.")
        return

    for entry in entries:
        if True:  # TODO: Update the conditional check to filter for selective videos
            details = get_video_details(entry["id"], ydl_opts)
            ts = details.get("timestamp")
            transcript = get_transcript(logger, entry["id"], ytt_api)
        break
    return ts, transcript


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    logger = logging.getLogger(__name__)

    ytt_api = YouTubeTranscriptApi()
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    details = check_and_extract(
        logger,
        channel_url="https://www.youtube.com/@SAMTIME/videos",
        ydl_opts=ydl_opts,
        ytt_api=ytt_api,
    )
