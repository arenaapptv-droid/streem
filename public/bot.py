import asyncio
import json
import os
import re
import shutil
import time
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
# PSUTIL
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

BASE_URL = "http://164.68.102.28"
PORT = 8080

HLS_DIR = "/tmp/hls"
STREAMS_FILE = "streams.json"

os.makedirs(HLS_DIR, exist_ok=True)

streams = {}
processes = {}

viewers = defaultdict(set)
viewer_last = defaultdict(dict)


if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        streams = json.load(f)


# =========================================
# SAVE
# =========================================
def save_streams():
    with open(STREAMS_FILE, "w") as f:
        json.dump(streams, f, indent=2)


# =========================================
# KEYBOARD
# =========================================
reply_kb = ReplyKeyboardMarkup([
    ["📺 قائمة HLS", "🖥 مراقبة السيرفر"],
    ["➕ إضافة بث", "🧹 تنظيف"]
], resize_keyboard=True)


# =========================================
# HTTP SERVER
# =========================================
async def track_viewer(request, sid):
    ip = request.headers.get("X-Forwarded-For", request.remote)
    if ip:
        viewers[sid].add(ip)
        viewer_last[sid][ip] = time.time()


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
# VIEWERS CLEANUP
# =========================================
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
# SERVER STATUS
# =========================================
def server_status():
    if not PSUTIL_AVAILABLE:
        return "psutil غير مثبت"

    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    return (
        f"🖥 CPU: {cpu}%\n"
        f"🧠 RAM: {ram.percent}%\n"
        f"💾 DISK: {disk.percent}%\n"
        f"📺 STREAMS: {len(streams)}"
    )


# =========================================
# START
# =========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        ["🧹 تنظيف"]
    ], resize_keyboard=True)

    context.user_data["current_sid"] = sid

    await update.message.reply_text(txt, reply_markup=kb)


# =========================================
# START STREAM
# =========================================
async def start_stream(sid, chat_id, bot):

    s = streams[sid]

    src = s["source"]
    mode = s.get("mode", "copy")
    ua = s.get("ua", "ExoPlayerLib/2.18.5")

    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)

    out_file = os.path.join(out_dir, "index.m3u8")

    cmd = [
        "ffmpeg",
        "-re",
        "-user_agent", ua,
        "-i", src
    ]

    if mode == "copy":
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]

    cmd += [
        "-c:a", "aac",
        "-b:a", "128k",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments",
        out_file
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )

    processes[sid] = proc
    streams[sid]["active"] = True
    streams[sid]["start_time"] = time.time()
    save_streams()

    async def logs():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            txt = line.decode(errors="ignore")
            m = re.search(r"fps=\s*([\d.]+)", txt)
            if m:
                streams[sid]["fps"] = m.group(1)

    asyncio.create_task(logs())

    await proc.wait()

    streams[sid]["active"] = False
    processes.pop(sid, None)
    save_streams()

    await bot.send_message(chat_id, f"⛔ توقف البث {s['name']}")


# =========================================
# STOP STREAM
# =========================================
async def stop_stream(sid):

    if sid in processes:
        proc = processes[sid]
        proc.terminate()
        await asyncio.sleep(1)
        proc.kill()
        processes.pop(sid, None)

    streams[sid]["active"] = False
    save_streams()

    path = os.path.join(HLS_DIR, sid)
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)


# =========================================
# HANDLE
# =========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "➕ إضافة بث":

        context.user_data["step"] = "name"
        await update.message.reply_text("أرسل اسم البث")

    elif context.user_data.get("step") == "name":

        sid = text.replace(" ", "_") + str(int(time.time()))

        streams[sid] = {
            "name": text,
            "source": "",
            "mode": "copy",
            "active": False,
            "fps": "?",
            "ua": "ExoPlayerLib/2.18.5"
        }

        context.user_data["sid"] = sid
        context.user_data["step"] = "source"

        save_streams()
        await update.message.reply_text("أرسل المصدر")

    elif context.user_data.get("step") == "source":

        sid = context.user_data["sid"]
        streams[sid]["source"] = text

        save_streams()
        context.user_data["step"] = None

        await show_panel(update, sid, context)

    elif text == "▶️ تشغيل":

        sid = context.user_data.get("current_sid")
        if sid:
            asyncio.create_task(start_stream(sid, chat_id, context.bot))
            await update.message.reply_text("▶️ تشغيل...")

    elif text == "⏹ إيقاف":

        sid = context.user_data.get("current_sid")
        if sid:
            await stop_stream(sid)
            await update.message.reply_text("⏹ توقف")

    elif text == "🧹 تنظيف":

        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        await update.message.reply_text("تم التنظيف")

    elif text == "🖥 مراقبة السيرفر":
        await update.message.reply_text(server_status())

    else:
        for sid, s in streams.items():
            if s["name"] == text:
                await show_panel(update, sid, context)
                return

        await update.message.reply_text("❌ غير معروف")


# =========================================
# MAIN (FIXED - IMPORTANT)
# =========================================
def main():

    loop = asyncio.get_event_loop()

    loop.create_task(start_http())
    loop.create_task(clean_viewers_loop())

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("BOT STARTED")

    app.run_polling()


if __name__ == "__main__":
    main()