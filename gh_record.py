
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

URL = os.getenv("STREAM_URL", "").strip()
HOURS = int(os.getenv("DURATION_HOURS", "4"))
SEGMENT_MB = int(os.getenv("SEGMENT_MB", "1900"))
OUTPUT = Path(os.getenv("OUTPUT_DIR", "output"))
BITRATE_KBPS = int(os.getenv("ESTIMATED_BITRATE_KBPS", "4000"))


def main():
    parsed = urlparse(URL)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SystemExit("A valid authorized HTTP/HTTPS URL is required.")

    if HOURS not in (4, 8):
        raise SystemExit("Duration must be 4 or 8 hours.")

    if not 100 <= SEGMENT_MB <= 1900:
        raise SystemExit("Segment size must be 100-1900 MB.")

    OUTPUT.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    pattern = OUTPUT / f"recording_{stamp}_part%03d.ts"

    # Approximate size: actual size varies with stream bitrate.
    seconds = max(
        60,
        int(SEGMENT_MB * 1024 * 1024 * 8 / (BITRATE_KBPS * 1000))
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-rw_timeout", "15000000",
        "-i", URL,
        "-t", str(HOURS * 3600),
        "-map", "0",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(seconds),
        "-reset_timestamps", "1",
        "-segment_format", "mpegts",
        str(pattern),
    ]

    print(f"Recording duration: {HOURS} hours")
    print(f"Approximate part target: {SEGMENT_MB} MB")

    subprocess.run(command, check=True)
    print("Recording completed.")


if __name__ == "__main__":
    main()
