import asyncio
import json
import os
import re
import shutil
import time
import signal
from collections import defaultdict

from aiohttp import web
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# =========================================
# OPTIONAL PSUTIL
# =========================================
try:
    import psutil
    PSUTIL_AVAILABLE = True
except:
    PSUTIL_AVAILABLE = False


# =========================================
# CONFIG
# =========================================
with open("settings.json") as f:
    cfg = json.load(f)

TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]

BASE_URL = cfg.get("BASE_URL", "http://127.0.0.1")
PORT = 8080

HLS_DIR = "/tmp/hls"
STREAMS_FILE = "streams.json"

os.makedirs(HLS_DIR, exist_ok=True)

streams = {}
processes = {}

# viewers
viewers = defaultdict(set)
viewer_last = defaultdict(dict)

# locks (مهم جدًا لمنع تشغيل مزدوج)
stream_locks = defaultdict(asyncio.Lock)


# =========================================
# LOAD STREAMS
# =========================================
if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        streams = json.load(f)


# =========================================
# SAVE STREAMS
# =========================================
def save_streams():
    with open(STREAMS_FILE, "w") as f:
        json.dump(streams, f, indent=2)


# =========================================
# KEYBOARD
# =========================================
reply_kb = ReplyKeyboardMarkup([
    ["📺 قائمة HLS", "📡 قائمة RTMP"],
    ["➕ إضافة بث", "🖥 مراقبة السيرفر"],
    ["♻️ إعادة تشغيل", "🧹 تنظيف"]
], resize_keyboard=True)


# =========================================
# VIEWERS TRACKING
# =========================================
async def track_viewer(request, sid):
    ip = request.headers.get("X-Forwarded-For", request.remote)
    if not ip:
        return
    now = time.time()
    viewers[sid].add(ip)
    viewer_last[sid][ip] = now


def clean_viewers():
    now = time.time()
    for sid in list(viewers.keys()):
        for ip in list(viewers[sid]):
            if now - viewer_last[sid].get(ip, 0) > 15:
                viewers[sid].discard(ip)
                viewer_last[sid].pop(ip, None)


async def clean_viewers_loop():
    while True:
        clean_viewers()
        await asyncio.sleep(10)


# =========================================
# HTTP SERVER
# =========================================
async def hls_handler(request):
    sid = request.match_info["name"]
    filename = request.match_info.get("file", "index.m3u8")

    path = os.path.join(HLS_DIR, sid, filename)

    if not os.path.exists(path):
        return web.Response(status=404)

    await track_viewer(request, sid)

    return web.FileResponse(path)


async def start_http():
    app = web.Application()
    app.router.add_get("/live/{name}/{file:.*}", hls_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"HTTP SERVER RUNNING ON {PORT}")


# =========================================
# SERVER STATUS
# =========================================
def server_status():
    if not PSUTIL_AVAILABLE:
        return "psutil غير مثبت"

    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()

    return (
        f"🖥 CPU: {cpu}%\n"
        f"🧠 RAM: {ram.percent}%\n"
        f"💾 DISK: {disk.percent}%\n"
        f"📤 OUT: {net.bytes_sent//1024//1024}MB\n"
        f"📥 IN: {net.bytes_recv//1024//1024}MB\n"
        f"📺 STREAMS: {len(streams)}"
    )


# =========================================
# START
# =========================================
async def start(update: Update, context):
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(
        "🎬 نظام البث المباشر",
        reply_markup=reply_kb
    )


# =========================================
# PANEL
# =========================================
async def show_panel(update, sid, context):
    s = streams[sid]

    uptime = "00:00:00"
    if s.get("start_time"):
        uptime = time.strftime(
            "%H:%M:%S",
            time.gmtime(time.time() - s["start_time"])
        )

    txt = (
        f"🎛 {s['name']}\n\n"
        f"📥 المصدر:\n{s['source']}\n\n"
        f"⚙️ الوضع: {s['mode']}\n"
        f"🟢 الحالة: {'يعمل' if s.get('active') else 'متوقف'}\n"
        f"🎬 FPS: {s.get('fps','?')}\n"
        f"👥 المشاهدين: {len(viewers[sid])}\n"
        f"⏱ التشغيل: {uptime}\n\n"
        f"🔗 {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    )

    kb = ReplyKeyboardMarkup([
        ["▶️ تشغيل", "⏹ إيقاف"],
        ["📥 تغيير المصدر", "🖼 تغيير الشعار"],
        ["🕵️ تغيير UA", "✏️ إعادة تسمية"],
        ["🔄 تبديل الوضع", "🗑 حذف البث"],
        ["🔙 القائمة الرئيسية"]
    ], resize_keyboard=True)

    context.user_data["current_sid"] = sid
    await update.message.reply_text(txt, reply_markup=kb)


# =========================================
# START STREAM (FIXED)
# =========================================
async def start_stream(sid, chat_id, bot):
    async with stream_locks[sid]:

        s = streams[sid]
        src = s["source"]

        if not src.startswith(("http://", "https://", "rtmp://")):
            return

        mode = s.get("mode", "copy")
        ua = s.get("ua", "ExoPlayer")

        logo = s.get("logo", "")

        out_dir = os.path.join(HLS_DIR, sid)
        os.makedirs(out_dir, exist_ok=True)

        out_file = os.path.join(out_dir, "index.m3u8")

        base = [
            "ffmpeg",

            "-threads", "2",
            "-async", "1",
            "-vsync", "1",

            "-user_agent", ua,

            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",

            "-fflags", "+genpts+discardcorrupt",
            "-err_detect", "ignore_err",
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "4096",

            "-i", src
        ]

        if mode == "copy":
            video = ["-c:v", "copy"]
        else:
            video = [
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-g", "50"
            ]

        audio = [
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2"
        ]

        if logo and mode != "copy":
            cmd = base + [
                "-i", logo,
                "-filter_complex",
                "[1:v]scale=120:-1[logo];[0:v][logo]overlay=10:10"
            ] + video + audio + [
                "-f", "hls",
                "-hls_time", "2",
                "-hls_list_size", "5",
                "-hls_flags", "delete_segments+append_list+omit_endlist",
                out_file
            ]
        else:
            cmd = base + video + audio + [
                "-f", "hls",
                "-hls_time", "2",
                "-hls_list_size", "5",
                "-hls_flags", "delete_segments+append_list+omit_endlist",
                out_file
            ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )

        processes[sid] = proc
        s["start_time"] = time.time()
        s["active"] = True
        save_streams()

        async def log_reader():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break

                txt = line.decode(errors="ignore")

                m = re.search(r"fps=\s*([\d.]+)", txt)
                if m:
                    s["fps"] = m.group(1)

        asyncio.create_task(log_reader())

        await proc.wait()

        processes.pop(sid, None)
        s["active"] = False
        save_streams()

        await bot.send_message(chat_id, f"⛔ توقف {s['name']}")


# =========================================
# STOP STREAM (FIXED)
# =========================================
async def stop_stream(sid):
    if sid in processes:
        proc = processes[sid]

        proc.terminate()

        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except:
            proc.kill()

        processes.pop(sid, None)

    if sid in streams:
        streams[sid]["active"] = False

    viewers.pop(sid, None)
    viewer_last.pop(sid, None)

    save_streams()

    shutil.rmtree(os.path.join(HLS_DIR, sid), ignore_errors=True)


# =========================================
# CLEANUP
# =========================================
async def cleanup_hls():
    while True:
        now = time.time()

        for sid in os.listdir(HLS_DIR):
            path = os.path.join(HLS_DIR, sid)

            if os.path.isdir(path):
                if now - os.path.getmtime(path) > 3600:
                    shutil.rmtree(path, ignore_errors=True)

        await asyncio.sleep(600)


async def cleanup_dead():
    while True:
        for sid in list(processes.keys()):
            if processes[sid].returncode is not None:
                processes.pop(sid, None)
        await asyncio.sleep(10)


# =========================================
# HANDLE MESSAGES
# =========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "▶️ تشغيل":
        sid = context.user_data.get("current_sid")
        if sid:
            streams[sid]["active"] = True
            asyncio.create_task(start_stream(sid, chat_id, context.bot))

    elif text == "⏹ إيقاف":
        sid = context.user_data.get("current_sid")
        if sid:
            await stop_stream(sid)

    elif text == "🧹 تنظيف":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)

    elif text == "♻️ إعادة تشغيل":
        for sid in streams:
            if streams[sid].get("active"):
                await stop_stream(sid)
                streams[sid]["active"] = True
                asyncio.create_task(start_stream(sid, chat_id, context.bot))


# =========================================
# MAIN FIXED
# =========================================
async def main():
    await start_http()

    asyncio.create_task(clean_viewers_loop())
    asyncio.create_task(cleanup_dead())
    asyncio.create_task(cleanup_hls())

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("BOT STARTED")

    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())