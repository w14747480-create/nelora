import os, re, time, uuid, threading
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file, send_from_directory, abort
from flask_cors import CORS
import yt_dlp

try:
    import curl_cffi  # noqa: F401
    from yt_dlp.networking.impersonate import ImpersonateTarget
    CAN_IMPERSONATE = True
except Exception:
    CAN_IMPERSONATE = False

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

TMP = "/tmp/vidora"
os.makedirs(TMP, exist_ok=True)

HOSTS = {
    "tiktok": ["tiktok.com"],
    "instagram": ["instagram.com", "instagr.am"],
    "snapchat": ["snapchat.com"],
}
# هەر کوالیتییەک چەند فۆرماتێکی یەدەگی هەیە، ئەگەر یەکەمیان نەبوو ئەوانی دیکە تاقی دەکرێنەوە
FORMATS = {
    "hd": ["bv*+ba/b", "b", "best"],
    "sd": ["b[height<=480]/w", "b", "best"],
}
MAX_BYTES = 200 * 1024 * 1024
FILE_TTL = 15 * 60
RATE_LIMIT = 8
hits = {}


def host_ok(url, platform):
    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(h == x or h.endswith("." + x) for x in HOSTS.get(platform, []))


def limited(ip):
    now = time.time()
    arr = [t for t in hits.get(ip, []) if now - t < 60]
    arr.append(now)
    hits[ip] = arr
    return len(arr) > RATE_LIMIT


def cleaner():
    while True:
        time.sleep(120)
        now = time.time()
        for f in os.listdir(TMP):
            p = os.path.join(TMP, f)
            try:
                if now - os.path.getmtime(p) > FILE_TTL:
                    os.remove(p)
            except OSError:
                pass


threading.Thread(target=cleaner, daemon=True).start()


def explain(err):
    s = str(err).lower()
    if any(k in s for k in ("login", "log in", "sign in", "cookies", "rate-limit", "rate limit", "empty media response")):
        return "ئەم پلاتفۆرمە داواکاری سێرڤەرەکەی ڕەتکردەوە (پێویستی بە لۆگین/کووکی هەیە). دواتر دووبارە هەوڵ بدەرەوە."
    if "private" in s or "not available" in s or "removed" in s or "unavailable" in s:
        return "ئەم ڤیدیۆیە تایبەتە یان سڕاوەتەوە. دڵنیابە گشتییە (Public)."
    if "unsupported url" in s:
        return "ئەم جۆرە لینکە پشتگیری ناکرێت. لینکی ڕاستەوخۆی ڤیدیۆکە بەکاربهێنە."
    if "ffmpeg" in s or "ffprobe" in s:
        return "ffmpeg لەسەر سێرڤەر دانەمەزراوە."
    if "larger than max-filesize" in s or "max-filesize" in s:
        return "ڤیدیۆکە زۆر گەورەیە."
    if "timed out" in s or "timeout" in s:
        return "سێرڤەر کاتی تەواو بوو. دووبارە هەوڵ بدەرەوە."
    return "نەتوانرا ڤیدیۆکە دابەزێنرێت. دڵنیابە لینکەکە گشتییە (Public) و دووبارە هەوڵ بدەرەوە."


def build_opts(vid, fmt, platform):
    opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(TMP, vid + ".%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_BYTES,
        "socket_timeout": 20,
        "retries": 3,
    }
    if CAN_IMPERSONATE and platform in ("tiktok", "instagram"):
        opts["impersonate"] = ImpersonateTarget("chrome")
    cookies = os.environ.get("COOKIES_FILE")
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    return opts


def find_file(vid):
    for f in os.listdir(TMP):
        if f.startswith(vid) and not f.endswith((".part", ".ytdl", ".temp")):
            return os.path.join(TMP, f)
    return None


@app.post("/api/download")
def download():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if limited(ip):
        return jsonify(error="داواکاری زۆرە، کەمێک چاوەڕێ بکە و دووبارە هەوڵ بدەرەوە."), 429

    d = request.get_json(silent=True) or {}
    url = (d.get("url") or "").strip()
    platform = d.get("platform", "")
    quality = d.get("quality", "hd")

    if not url or not host_ok(url, platform):
        return jsonify(error="لینکەکە دروست نییە یان لەگەڵ پلاتفۆرمەکە ناگونجێت."), 400

    vid = uuid.uuid4().hex
    info, path, last_err = None, None, None

    for fmt in FORMATS.get(quality, FORMATS["hd"]):
        try:
            with yt_dlp.YoutubeDL(build_opts(vid, fmt, platform)) as ydl:
                info = ydl.extract_info(url, download=True)
            path = find_file(vid)
            if path:
                break
            last_err = RuntimeError("no file produced")
        except Exception as e:
            last_err = e
            app.logger.warning("download failed (%s): %s", fmt, e)
            if "format" not in str(e).lower():
                break  # هەڵەکە پەیوەندی بە فۆرماتەوە نییە، دووبارە تاقی نەکەینەوە

    if not path:
        return jsonify(error=explain(last_err), detail=str(last_err)[-300:]), 422

    title = re.sub(r"[^\w\- ]+", "", (info or {}).get("title") or "vidora")[:50].strip() or "vidora"
    ext = os.path.splitext(path)[1]
    return jsonify(download_url=f"/api/file/{os.path.basename(path)}?name={title}")


@app.get("/api/file/<name>")
def file(name):
    if not re.fullmatch(r"[0-9a-f]{32}\.[A-Za-z0-9]{2,5}", name):
        abort(404)
    p = os.path.join(TMP, name)
    if not os.path.exists(p):
        abort(404)
    title = re.sub(r"[^\w\- ]+", "", request.args.get("name", "vidora"))[:50] or "vidora"
    return send_file(p, as_attachment=True, download_name=f"{title}{os.path.splitext(name)[1]}")


@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/health")
def health():
    return jsonify(ok=True, impersonate=CAN_IMPERSONATE, yt_dlp=yt_dlp.version.__version__)
