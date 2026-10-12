
#!/usr/bin/env python3
import os
import sys
import time
import json
import signal
import asyncio
import threading
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path

from pyrogram import Client

# ---------- Configuration ----------
TOKEN = os.environ["TG_TOKEN"]
CHAT = os.environ["TG_CHAT"]
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
URL = os.environ["URL"]

NAME = os.getenv("NAME", "Recording")
MINUTES = float(os.getenv("MINUTES", "60"))
PART_MIN = float(os.getenv("PART_MIN", "10"))
OUT = Path(os.getenv("OUT_DIR", "./rec"))
MAX_UPLOAD_BYTES = 1900 * 1024 * 1024

OUT.mkdir(parents=True, exist_ok=True)
STOP = threading.Event()
START = time.time()
CURRENT = None
BOT_API = f"https://api.telegram.org/bot{TOKEN}/"

def bot_call(method, data=None, timeout=30):
    req = urllib.request.Request(
        BOT_API + method,
        data=urllib.parse.urlencode(data or {}).encode()
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def send_text(text):
    try:
        bot_call("sendMessage", {"chat_id": CHAT, "text": text})
    except Exception as e:
        print("Telegram message error:", repr(e), flush=True)

def status_text():
    elapsed = int(time.time() - START)
    return (
        f"{NAME}\n"
        f"Elapsed: {elapsed // 60} min\n"
        f"Recording: {'running' if not STOP.is_set() else 'stopping'}"
    )

def command_listener():
    # This listener accepts commands only from the configured chat.
    offset = 0
    while not STOP.is_set():
        try:
            result = bot_call("getUpdates", {
                "offset": offset,
                "timeout": 20,
                "allowed_updates": json.dumps(["message"])
            }, timeout=30)

            for update in result.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip().lower()

                if chat_id != str(CHAT):
                    continue
                if text == "/status":
                    send_text(status_text())
                elif text == "/stop":
                    send_text("Stopping recording and saving the current part...")
                    STOP.set()
                    p = CURRENT
                    if p and p.poll() is None:
                        p.send_signal(signal.SIGINT)
                    return

        except Exception as e:
            print("Command polling error:", repr(e), flush=True)
            time.sleep(5)

async def upload_file(app, path):
    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        send_text(
            f"File too large for this script's upload limit: {path.name}\n"
            "The file has been kept on the VPS."
        )
        return False

    caption = f"{NAME} — {path.name}"
    try:
        if path.suffix.lower() == ".mp4":
            await app.send_video(
                chat_id=int(CHAT),
                video=str(path),
                caption=caption,
                supports_streaming=True
            )
        else:
            await app.send_document(
                chat_id=int(CHAT),
                document=str(path),
                caption=caption
            )

        print("Telegram upload successful:", path.name, flush=True)
        path.unlink(missing_ok=True)
        return True
    except Exception as e:
        print("Telegram upload failed:", repr(e), flush=True)
        send_text(
            f"Upload failed: {path.name}\n"
            "The file has been kept on the VPS. Check recorder.log."
        )
        return False

async def uploader():
    async with Client(
        "recorder_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=TOKEN,
        in_memory=True,
        no_updates=True
    ) as app:
        while True:
            files = sorted(OUT.glob("*.ts"))
            # Upload only completed parts; the active file is not processed.
            for path in files:
                if path.name.endswith(".active.ts"):
                    continue
                mp4 = path.with_suffix(".mp4")
                result = subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-y", "-i", str(path),
                    "-c", "copy", "-movflags", "+faststart", str(mp4)
                ], capture_output=True, text=True)

                if result.returncode == 0 and mp4.exists() and mp4.stat().st_size:
                    path.unlink(missing_ok=True)
                    await upload_file(app, mp4)
                else:
                    print("Remux failed:", result.stderr[-1000:], flush=True)
                    # Keep the original .ts file for recovery.
                    path.rename(path.with_suffix(".failed.ts"))

            if STOP.is_set() and not list(OUT.glob("*.ts")):
                break
            await asyncio.sleep(5)

def record():
    global CURRENT

    # Validate access without attempting to bypass restrictions.
    test = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rw_timeout", "15000000",
        "-i", URL, "-t", "3", "-f", "null", "-"
    ], capture_output=True, text=True, timeout=30)

    if test.returncode != 0:
        raise RuntimeError(
            "Cannot read this HLS URL. Check that it is valid and authorized.\n"
            + test.stderr[-1500:]
        )

    send_text(f"Recording started: {NAME}\nSend /status or /stop.")

    listener = threading.Thread(target=command_listener, daemon=True)
    listener.start()

    started = time.time()
    deadline = started + MINUTES * 60 if MINUTES > 0 else None
    part_seconds = max(60, int(PART_MIN * 60))
    part_number = 1

    while not STOP.is_set():
        if deadline and time.time() >= deadline:
            break

        remaining = int(deadline - time.time()) if deadline else part_seconds
        duration = min(part_seconds, remaining) if deadline else part_seconds
        if duration <= 0:
            break

        active = OUT / f"{NAME}_{part_number:04d}.active.ts"
        final = OUT / f"{NAME}_{part_number:04d}.ts"

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-y", "-rw_timeout", "30000000",
            "-i", URL,
            "-map", "0:v?", "-map", "0:a?",
            "-c", "copy", "-t", str(duration),
            "-f", "mpegts", str(active)
        ]

        CURRENT = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True
        )
        _, stderr = CURRENT.communicate()
        code = CURRENT.returncode
        CURRENT = None

        if active.exists() and active.stat().st_size > 0:
            active.rename(final)
            print("Part completed:", final, flush=True)
        else:
            active.unlink(missing_ok=True)

        if STOP.is_set() or (deadline and time.time() >= deadline):
            break

        if code != 0:
            print("FFmpeg stopped:", (stderr or "")[-1000:], flush=True)
            # Do not loop forever if the URL is no longer accessible.
            if "403 Forbidden" in (stderr or "") or "401 Unauthorized" in (stderr or ""):
                raise RuntimeError("Stream access was denied; recording stopped.")
            time.sleep(5)

        part_number += 1

    STOP.set()
    send_text("Recording stopped. Uploading remaining completed parts...")
    asyncio.run(uploader())
    send_text("Recording process finished.")

def handle_signal(signum, frame):
    STOP.set()
    p = CURRENT
    if p and p.poll() is None:
        p.send_signal(signal.SIGINT)

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

if __name__ == "__main__":
    try:
        record()
    except Exception as e:
        print("FATAL:", repr(e), flush=True)
        send_text(f"Recording error:\n{str(e)[:2500]}")
        sys.exit(1)
