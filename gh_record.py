#!/usr/bin/env python3
"""Runs inside GitHub Actions.
Records an HLS link with ffmpeg -c copy (no quality loss) in ONE continuous stream, cuts it into files
(every PART_MIN minutes, at most PART_MAX_MB, or when you press Split) at keyframes, uploads each finished
file to a GitHub Release (and to Telegram if TG_API_ID/TG_API_HASH are set) while the recording keeps going.
  python gh_record.py            record
  python gh_record.py finish     upload anything left over (used by the always() step)"""
import os, re, sys, json, glob, time, html, queue, signal, shutil, threading, subprocess, urllib.request, urllib.parse, urllib.error

E = os.environ.get
TOKEN, CHAT = E("TG_TOKEN", ""), E("TG_CHAT", "")
REPO, RUN, SHA = E("REPO", ""), E("RUN_ID", "local"), E("SHA", "")
URL = E("URL", "")
NAME = re.sub(r"[^\w\-]+", "_", E("NAME") or "ClipByNaim")
MINUTES = float(E("MINUTES") or 0)             # 0 = until Stop
PART_MIN = float(E("PART_MIN") or 60)           # a new file every N minutes of video
PART_MAX = float(E("PART_MAX_MB") or 1900) * 1e6  # ...or when a file reaches this size (GitHub allows 2 GB per file)
BACK = E("BACK_SEGS") or "5"                    # start N segments behind live (safety)
OUT = E("OUT_DIR") or "rec"
FAILED = E("FAILED_DIR") or "rec_failed"
DRY = E("DRY") == "1"                           # test mode: no uploads
TEST_ONCE = E("TEST_ONCE") == "1"               # test mode: do not restart when the input ends
TAG = "rec-" + RUN
LIMIT = 345 * 60                                # one job runs ~6 h; a new run continues before that
STAMP = time.strftime("%Y%m%d_%H%M%S")
REL_URL = "https://github.com/%s/releases/tag/%s" % (REPO, TAG)
RUN_URL = "https://github.com/%s/actions/runs/%s" % (REPO, RUN)
UA = E("UA") or "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
REFERER = E("REFERER") or ""
API_ID, API_HASH = E("TG_API_ID", ""), E("TG_API_HASH", "")   # optional: lets the bot upload files up to 2 GB
BIG_LIMIT = 1950 * 1000 * 1000
HDR = ["-user_agent", UA]

stop = threading.Event()
S = {"proc": None, "t0": time.time(), "restarts": 0, "done": [], "rel_err": "", "split_msgs": 0}
upq = queue.Queue()
SP = None


# ---------------- small helpers ----------------
def esc(t):
    return html.escape(str(t), quote=False)


def human(b):
    return "%.2f GB" % (b / 1e9) if b >= 1e9 else "%.0f MB" % (b / 1e6)


def hms(s):
    s = int(max(0, s))
    return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def mmss(s):
    s = int(max(0, s))
    return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60) if s >= 3600 else "%d:%02d" % (s // 60, s % 60)


def mask(t):
    return (t or "").replace(URL, "<link>") if URL else (t or "")


def _origin(u):
    m = re.match(r"^(https?://[^/]+)", u or "")
    return m.group(1) if m else ""


KB = {"inline_keyboard": [[{"text": "📊 Status", "callback_data": "status"},
                           {"text": "✂️ Split", "callback_data": "split"},
                           {"text": "⏹ Stop", "callback_data": "stop"}]]}


def tg(method, **data):
    if not TOKEN or DRY:
        print("[telegram]", method, re.sub(r"<[^>]+>", "", str(data.get("text", ""))).replace("\n", " | "))
        return {}
    try:
        req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (TOKEN, method), urllib.parse.urlencode(data).encode())
        return json.load(urllib.request.urlopen(req, timeout=40))
    except urllib.error.HTTPError as e:
        try: body = e.read().decode()[:200]
        except Exception: body = ""
        print("Telegram error %s: %s" % (e.code, body), flush=True)
        return {"ok": False, "error_code": e.code}
    except Exception as e:
        print("telegram error:", e)
        return {}


def say(text, buttons=False):
    if not (CHAT or DRY):
        print("[msg]", re.sub(r"<[^>]+>", "", text))
        return
    d = dict(chat_id=CHAT, text=text, parse_mode="HTML", disable_web_page_preview="true")
    if buttons:
        d["reply_markup"] = json.dumps(KB)
    tg("sendMessage", **d)


def detect_chat():
    """If TG_CHAT is empty, use the chat of the latest message sent to the bot (send it 'hi' first)."""
    global CHAT
    if CHAT or not TOKEN or DRY:
        return
    r = tg("getUpdates", timeout=0)
    ids = [((u.get("message") or {}).get("chat") or {}).get("id") for u in r.get("result", [])]
    ids = [i for i in ids if i]
    if ids:
        CHAT = str(ids[-1]); print("Chat detected from the latest message sent to the bot.", flush=True)
    else:
        print("NO CHAT ID: send 'hi' to your bot in Telegram, then run again (within 24 hours).", flush=True)


def summary(text):
    p = E("GITHUB_STEP_SUMMARY")
    if p:
        try:
            with open(p, "a") as f:
                f.write(text + "\n")
        except OSError:
            pass


# ---------------- MPEG-TS splitter (cuts at keyframes, recording never stops) ----------------
def pes_pts(pk):
    afc = (pk[3] >> 4) & 3
    if not afc & 1:
        return None
    p = 4 + ((1 + pk[4]) if afc & 2 else 0)
    if p + 14 > 188 or pk[p] or pk[p + 1] or pk[p + 2] != 1 or not (pk[p + 7] & 0x80):
        return None
    return ((pk[p + 9] >> 1) & 7) * 1073741824 + ((pk[p + 10] << 22) | ((pk[p + 11] >> 1) << 15) | (pk[p + 12] << 7) | (pk[p + 13] >> 1))


class Splitter:
    def __init__(self, on_done):
        self.on_done = on_done
        self.buf = b""
        self.f = None; self.path = ""
        self.part_no = 0; self.cur_bytes = 0; self.total = 0
        self.start_pts = None; self.last_pts = None
        self.pat = None; self.pmt = None; self.pmt_pid = None; self.vid = None
        self.want_split = False; self.manual_flag = False; self.on_manual = None
        self.hist = []

    def media_secs(self):
        if self.start_pts is None or self.last_pts is None:
            return 0.0
        return ((self.last_pts - self.start_pts) % (1 << 33)) / 90000.0

    def speed_mbps(self):
        now = time.time()
        self.hist = [h for h in self.hist if now - h[0] <= 12]
        if len(self.hist) < 2:
            return 0.0
        dt = self.hist[-1][0] - self.hist[0][0]
        return (self.hist[-1][1] - self.hist[0][1]) * 8 / dt / 1e6 if dt > 0.5 else 0.0

    def _open(self):
        self.part_no += 1
        self.path = os.path.join(OUT, "%s_%s_part%03d.ts" % (NAME, STAMP, self.part_no))
        self.f = open(self.path, "wb")
        self.cur_bytes = 0
        self.start_pts = self.last_pts
        for p in (self.pat, self.pmt):                 # every file starts with PAT + PMT, then a keyframe
            if p:
                self.f.write(p); self.cur_bytes += 188

    def _close(self):
        if not self.f:
            return
        self.f.close(); self.f = None
        if self.cur_bytes > 188 * 40:
            self.on_done(self.path)
        else:
            try: os.remove(self.path)
            except OSError: pass

    def finish(self):
        self._close()

    def _pat(self, pk):
        self.pat = bytes(pk)
        p = 4 + ((1 + pk[4]) if (pk[3] >> 4) & 2 else 0)
        p += 1 + pk[p]
        sec = ((pk[p + 1] & 15) << 8) | pk[p + 2]
        q, end = p + 8, min(p + 3 + sec - 4, 188)
        while q + 4 <= end:
            if ((pk[q] << 8) | pk[q + 1]) != 0:
                self.pmt_pid = ((pk[q + 2] & 31) << 8) | pk[q + 3]
                break
            q += 4

    def _pmt(self, pk):
        self.pmt = bytes(pk)
        p = 4 + ((1 + pk[4]) if (pk[3] >> 4) & 2 else 0)
        p += 1 + pk[p]
        sec = ((pk[p + 1] & 15) << 8) | pk[p + 2]
        info = ((pk[p + 10] & 15) << 8) | pk[p + 11]
        q, end = p + 12 + info, min(p + 3 + sec - 4, 188)
        while q + 5 <= end:
            if pk[q] in (0x1b, 0x24, 0x02, 0x10) and self.vid is None:
                self.vid = ((pk[q + 1] & 31) << 8) | pk[q + 2]
            q += 5 + (((pk[q + 3] & 15) << 8) | pk[q + 4])

    def _should_split(self):
        if self.want_split:
            self.want_split = False; self.manual_flag = True
            return True
        return self.media_secs() >= PART_MIN * 60 or self.cur_bytes >= PART_MAX

    def feed(self, data):
        self.buf += data
        n = len(self.buf); i = 0
        while i < n and self.buf[i] != 0x47:
            i += 1
        end = i + ((n - i) // 188) * 188
        buf = self.buf
        for o in range(i, end, 188):
            pk = buf[o:o + 188]
            pid = ((pk[1] & 0x1f) << 8) | pk[2]
            if pid == 0x1fff:
                continue                                  # drop empty padding packets
            pusi = pk[1] & 0x40
            key = False
            if pid == 0 and pusi:
                self._pat(pk)
                if self.vid is None and self.pmt:
                    key = True                            # audio-only stream: cut at PAT
            elif pid == self.pmt_pid and pusi:
                self._pmt(pk)
            elif pid == self.vid and pusi:
                pts = pes_pts(pk)
                if pts is not None:
                    self.last_pts = pts
                if (pk[3] >> 4) & 2 and pk[4] > 0 and (pk[5] & 0x40):
                    key = True                            # random access indicator = keyframe starts here
            if self.f is None:
                if (key or (self.vid is None and self.pmt and pid not in (0, self.pmt_pid))) and (self.pat and self.pmt):
                    self._open()
                else:
                    continue
            elif key and self._should_split():
                self._close(); self._open()
                if self.manual_flag:
                    self.manual_flag = False
                    if self.on_manual:
                        self.on_manual(self.part_no)
            self.f.write(pk); self.cur_bytes += 188
        self.total += end - i
        self.buf = buf[end:]
        self.hist.append((time.time(), self.total))


# ---------------- messages ----------------
def status_text():
    el = time.time() - S["t0"]
    left = max(0, MINUTES * 60 - el) if MINUTES else None
    sp = SP
    saved = sum(d["size"] for d in S["done"])
    spd = sp.speed_mbps() if sp else 0
    nxt = ""
    if sp and sp.f:
        t_left = PART_MIN * 60 - sp.media_secs()
        if spd > 0.1:
            t_left = min(t_left, (PART_MAX - sp.cur_bytes) * 8 / (spd * 1e6))
        nxt = "\n✂️ <b>Next auto-split</b>  in ~%s" % mmss(t_left)
    lines = [
        "🔴 <b>Recording</b>  ·  %s" % esc(NAME), "",
        "⏱ <b>Elapsed</b>  %s%s" % (hms(el), ("   ·   left %s" % hms(left)) if left is not None else ""),
        "📦 <b>Current file</b>  #%02d  ·  %s  ·  %s" % (sp.part_no if sp else 0, human(sp.cur_bytes) if sp else "0 MB", mmss(sp.media_secs()) if sp else "0:00"),
        "✅ <b>Saved</b>  %d file(s)  ·  %s" % (len(S["done"]), human(saved)),
        "⚡ <b>Speed</b>  %.1f Mbps" % spd + nxt,
    ]
    if S["restarts"]:
        lines.append("♻️ <b>Reconnects</b>  %d" % S["restarts"])
    lines += ["", '🔗 <a href="%s">Open files</a>' % REL_URL]
    return "\n".join(lines)


def do_action(act, cq_id=None):
    global SP
    if act == "status":
        txt = "Status sent"; say(status_text(), buttons=True)
    elif act == "split":
        if SP and SP.f:
            SP.want_split = True; txt = "Split requested"
            say("✂️ <b>Split requested</b>\nThe current file is closed at the next keyframe (a few seconds). Recording continues in a new file.", buttons=True)
        else:
            txt = "Not recording yet"; say("Recording has not started yet, nothing to split.")
    elif act == "stop":
        txt = "Stopping"
        say("⏹ <b>Stopping...</b>\nSaving the last file. This can take up to a minute.")
        halt()
    else:
        return
    if cq_id:
        tg("answerCallbackQuery", callback_query_id=cq_id, text=txt)


def halt():
    stop.set()
    p = S["proc"]
    if p and p.poll() is None:
        p.send_signal(signal.SIGINT)                  # ffmpeg finishes cleanly


def poll():
    off = 0
    r = tg("getUpdates", offset=-1, timeout=0)
    if r.get("error_code") == 409:
        return                                         # a webhook (worker) is active: it handles /stop
    for u in r.get("result", []):
        off = u["update_id"] + 1
    while not stop.is_set():
        r = tg("getUpdates", offset=off, timeout=15, allowed_updates=json.dumps(["message", "callback_query"]))
        if r.get("error_code") == 409:
            return
        for u in r.get("result", []):
            off = u["update_id"] + 1
            cq = u.get("callback_query")
            if cq:
                if str(((cq.get("message") or {}).get("chat") or {}).get("id")) == str(CHAT):
                    do_action(cq.get("data", ""), cq.get("id"))
                continue
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != str(CHAT):
                continue
            t = (m.get("text") or "").strip().lower().split("@")[0]
            for key in ("status", "split", "stop"):
                if t.startswith("/" + key) or (key == "split" and t.startswith("/slipt")):
                    do_action(key); break
            else:
                if t.startswith("/start") or t.startswith("/help"):
                    say("🎬 <b>ClipByNaim recorder</b>\nUse the buttons or send /status, /split, /stop.", buttons=True)
        if not r:
            time.sleep(3)


# ---------------- upload ----------------
def ensure_release(force=False):
    if DRY or not shutil.which("gh") or not REPO:
        return True
    if not force and subprocess.run(["gh", "release", "view", TAG, "--repo", REPO], capture_output=True).returncode == 0:
        return True
    cmd = ["gh", "release", "create", TAG, "--repo", REPO, "--title", "Recording " + STAMP, "--notes", NAME]
    if SHA:
        cmd += ["--target", SHA]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and "already exists" not in (r.stderr or ""):
        S["rel_err"] = (r.stderr or r.stdout or "").strip()[:300]
        print("gh release create failed:", S["rel_err"], flush=True)
        return False
    S["rel_err"] = ""
    return True


def gh_upload(path):
    if DRY or not shutil.which("gh") or not REPO:
        print("[dry] would upload", path, human(os.path.getsize(path)))
        return True, ""
    err = ""
    for attempt in range(3):
        ensure_release()
        r = subprocess.run(["gh", "release", "upload", TAG, path, "--repo", REPO, "--clobber"], capture_output=True, text=True)
        if r.returncode == 0:
            return True, ""
        err = (r.stderr or r.stdout or "").strip() or S["rel_err"]
        print("gh release upload failed (try %d): %s" % (attempt + 1, err), flush=True)
        time.sleep(5 * (attempt + 1))
    return False, err


def media_info(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                            "-of", "json", path], capture_output=True, text=True, timeout=60)
        j = json.loads(r.stdout or "{}"); st = (j.get("streams") or [{}])[0]
        return float((j.get("format") or {}).get("duration") or 0), int(st.get("width") or 0), int(st.get("height") or 0)
    except Exception:
        return 0.0, 0, 0


def tg_big(path, caption):
    """Upload up to ~2 GB to your chat as the bot (MTProto). None = not configured, True = sent, False = failed."""
    if not (API_ID and API_HASH and TOKEN and CHAT) or DRY:
        return None
    if os.path.getsize(path) > BIG_LIMIT:
        print("File is larger than 1.95 GB, not sending to Telegram.", flush=True)
        return False
    try:
        import asyncio
        from pyrogram import Client
    except Exception as e:
        print("Telegram uploader missing:", e, flush=True)
        return False
    dur, w, h = media_info(path)
    thumb = path + ".jpg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", path, "-frames:v", "1", "-vf", "scale=320:-2", thumb], capture_output=True)

    async def go():
        async with Client("rec", api_id=int(API_ID), api_hash=API_HASH, bot_token=TOKEN, in_memory=True, no_updates=True) as app:
            if path.endswith(".mp4") and w:
                kw = dict(caption=caption, supports_streaming=True, duration=int(dur), width=w, height=h)
                if os.path.exists(thumb):
                    kw["thumb"] = thumb
                await app.send_video(int(CHAT), path, **kw)
            else:
                await app.send_document(int(CHAT), path, caption=caption)
    ok = True
    try:
        asyncio.run(go())
    except Exception as e:
        print("Telegram upload failed:", repr(e), flush=True)
        ok = False
    try: os.remove(thumb)
    except OSError: pass
    return ok


def process(ts):
    """remux the finished .ts to .mp4 (proper duration), upload it, tell Telegram, clean up."""
    mp4 = ts[:-3] + ".mp4"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", ts, "-c", "copy", "-movflags", "+faststart", mp4],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(mp4):
        mp4 = ts
    size = os.path.getsize(mp4)
    dur = media_info(mp4)[0]
    n = len(S["done"]) + 1
    base = os.path.basename(mp4)
    ok, err = gh_upload(mp4)
    big = tg_big(mp4, "%s  ·  %s" % (base, mmss(dur))) if ok else None
    head = "✅ <b>File %d saved</b>\n🎞 %s  ·  %s\n" % (n, mmss(dur), human(size))
    if ok:
        S["done"].append({"name": base, "size": size, "secs": dur})
        if big:
            say(head + "📨 Sent here as a video\n🔗 <a href=\"%s\">Backup copy</a>" % REL_URL)
        elif big is False:
            say(head + "⚠️ Could not send it to Telegram (see the run log)\n🔗 <a href=\"%s\">Download from GitHub</a>" % REL_URL)
        else:
            say(head + "🔗 <a href=\"%s\">Download from GitHub</a>" % REL_URL)
        if big is None and size <= 49 * 1024 * 1024 and TOKEN and CHAT and not DRY:
            subprocess.run(["curl", "-s", "-F", "chat_id=" + CHAT, "-F", "document=@" + mp4,
                            "https://api.telegram.org/bot%s/sendDocument" % TOKEN], capture_output=True)
        if not DRY:
            for f in {ts, mp4}:
                try: os.remove(f)
                except OSError: pass
    else:
        os.makedirs(FAILED, exist_ok=True)
        try: shutil.move(mp4, os.path.join(FAILED, base))
        except OSError: pass
        S["done"].append({"name": base, "size": size, "secs": dur})
        say("⚠️ <b>Upload failed</b> for %s (%s)\n<b>Reason:</b> <code>%s</code>\n\nThe file is kept. Open the run page, scroll to <b>Artifacts</b> after the run ends and download it:\n%s\n\n"
            "If the reason says <i>Resource not accessible by integration</i>: Settings > Actions > General > Workflow permissions > Read and write." %
            (esc(base), human(size), esc((err or "unknown")[:250]), RUN_URL))


def uploader():
    ensure_release()
    if S["rel_err"]:
        say("⚠️ Could not create the GitHub release: <code>%s</code>" % esc(S["rel_err"]))
    while True:
        item = upq.get()
        if item is None:
            return
        try:
            process(item)
        except Exception as e:
            print("process error:", repr(e), flush=True)
            say("⚠️ Error while saving a file: <code>%s</code>" % esc(repr(e)[:200]))


# ---------------- link check + recording ----------------
def explain(err):
    e = (err or "").lower()
    if "403" in e or "forbidden" in e:
        return "403 Forbidden: the stream server refuses GitHub's server. Country/IP block, or the link needs a login or an expired token."
    if "401" in e or "unauthorized" in e:
        return "401 Unauthorized: the link needs a login or its token is wrong or expired."
    if "404" in e or "not found" in e:
        return "404 Not found: the link has expired or is wrong. Copy a fresh link and try again."
    if "timed out" in e or "timeout" in e:
        return "Connection timed out: the server does not answer GitHub's server. Usually an IP/country block."
    if "could not resolve" in e or "name or service not known" in e or "nodename" in e:
        return "The website name in the link could not be found. Check the link for typing mistakes."
    if "invalid data found" in e:
        return "This link is not a video playlist (.m3u8). Open it in a browser: it should give a playlist, not a web page."
    if "connection refused" in e or "reset by peer" in e or "end of file" in e:
        return "The server closed the connection. Usually it blocks GitHub's server."
    return "Unknown reason. Read the ffmpeg line above."


def header_sets():
    sets = []
    if REFERER:
        o = _origin(REFERER)
        sets.append(("the referer you gave", ["-user_agent", UA, "-referer", REFERER] + (["-headers", "Origin: %s\r\n" % o] if o else [])))
    sets.append(("a normal browser User-Agent", ["-user_agent", UA]))
    o = _origin(URL)
    if o:
        sets.append(("browser headers with the stream's own site as referer", ["-user_agent", UA, "-referer", o + "/", "-headers", "Origin: %s\r\n" % o]))
    return sets


def precheck():
    global HDR
    last = ""
    for label, hdr in header_sets():
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error"] + hdr + ["-t", "6", "-i", URL, "-f", "null", "-"],
                               capture_output=True, text=True, timeout=90)
        except Exception as e:
            last = str(e); continue
        if r.returncode == 0:
            HDR = hdr
            print("Link opened using: " + label, flush=True)
            return True, ""
        last = mask((r.stderr or "").strip())[-400:]
        print("Could not open with %s: %s" % (label, last.splitlines()[-1] if last else "?"), flush=True)
    return False, last


def where():
    try:
        d = json.load(urllib.request.urlopen("https://ipinfo.io/json", timeout=8))
        return "%s, %s" % (d.get("city", "?"), d.get("country", "?"))
    except Exception:
        return "unknown"


def chain(rem_minutes):
    time.sleep(LIMIT - 300)
    if stop.is_set() or not shutil.which("gh"):
        return
    say("⏳ The 6 hour job limit is near. Starting the next run to continue...")
    cmd = ["gh", "workflow", "run", "record.yml", "--repo", REPO, "-f", "url=" + URL, "-f", "minutes=%d" % rem_minutes,
           "-f", "name=" + NAME, "-f", "part_min=%s" % PART_MIN]
    if REFERER:
        cmd += ["-f", "referer=" + REFERER]
    subprocess.run(cmd)


def stderr_reader(p, box):
    for line in iter(p.stderr.readline, b""):
        box.append(line.decode("utf-8", "ignore"))
        del box[:-30]


def record():
    global SP
    os.makedirs(OUT, exist_ok=True)
    detect_chat()
    ok, err = precheck()
    if not ok:
        why = explain(err)
        print("PRECHECK FAILED. ffmpeg said:\n" + err, flush=True)
        print("::error title=Link could not be opened from GitHub::" + why, flush=True)
        summary("## Recording did not start\n**Reason:** " + why + "\n\n```\n" + err + "\n```")
        say("❌ <b>The link could not be opened from GitHub's server</b>\n<b>Reason:</b> %s\n\n<code>%s</code>" % (esc(why), esc(err[-300:])))
        sys.exit(1)
    plan = ("stops after %d min" % MINUTES) if MINUTES else "runs until you press Stop"
    say("🔴 <b>Recording started</b>\n🎬 %s\n⏱ %s\n✂️ New file every %g min (max %s)\n📍 GitHub server: %s\n🔗 <a href=\"%s\">Files will appear here</a>" %
        (esc(NAME), plan, PART_MIN, human(PART_MAX), esc(where()), REL_URL), buttons=True)
    signal.signal(signal.SIGINT, lambda *a: halt())
    signal.signal(signal.SIGTERM, lambda *a: halt())
    total = MINUTES * 60
    if not total or total > LIMIT:
        threading.Thread(target=chain, args=(int((total - LIMIT) / 60) if total else 0,), daemon=True).start()
    if TOKEN and CHAT and not DRY:
        threading.Thread(target=poll, daemon=True).start()
    up = threading.Thread(target=uploader)
    up.start()
    SP = Splitter(upq.put)
    SP.on_manual = lambda n: say("✂️ <b>Split done</b>\nFile #%d started. Recording continues." % n, buttons=True)
    end_at = S["t0"] + (min(total, LIMIT) if total else LIMIT)

    def watchdog():
        while not stop.is_set():
            if time.time() >= end_at:
                halt(); return
            time.sleep(2)
    threading.Thread(target=watchdog, daemon=True).start()

    last_err = []
    while not stop.is_set():
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-rw_timeout", "30000000", "-live_start_index", "-" + BACK] + HDR + [
               "-i", URL, "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-f", "mpegts", "-mpegts_flags", "+resend_headers", "pipe:1"]
        p = S["proc"] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        last_err = []
        threading.Thread(target=stderr_reader, args=(p, last_err), daemon=True).start()
        while True:
            data = p.stdout.read(188 * 1024)
            if not data:
                break
            SP.feed(data)
        p.wait()
        if stop.is_set() or TEST_ONCE:
            break
        S["restarts"] += 1
        SP.want_split = True                          # after a reconnect the picture restarts: begin a new file
        if S["restarts"] == 1 or S["restarts"] % 5 == 0:
            say("♻️ <b>Stream dropped or ended</b>, reconnecting (%d)...\n<code>%s</code>" % (S["restarts"], esc(mask("".join(last_err)).strip()[-200:])))
        time.sleep(5)
    SP.finish()
    upq.put(None)
    up.join()
    saved = sum(d["size"] for d in S["done"])
    summary("## Recording finished\n%d file(s), %s. [Open the release](%s)" % (len(S["done"]), human(saved), REL_URL))
    say("🏁 <b>Recording finished</b>\n🎬 %s\n📁 %d file(s)  ·  %s\n⏱ %s\n🔗 <a href=\"%s\">Download</a>" %
        (esc(NAME), len(S["done"]), human(saved), hms(time.time() - S["t0"]), REL_URL))


def finish():
    """always() step: upload leftovers if the main step was cancelled or killed."""
    leftovers = sorted(glob.glob(os.path.join(OUT, "*.ts")))
    if not leftovers:
        return
    ensure_release()
    for f in leftovers:
        process(f)
    say("⚠️ <b>Recording was interrupted.</b> Saved %d leftover file(s).\n🔗 <a href=\"%s\">Download</a>" % (len(leftovers), REL_URL))


if __name__ == "__main__":
    if not URL:
        sys.exit("URL env var is empty")
    finish() if len(sys.argv) > 1 and sys.argv[1] == "finish" else record()
