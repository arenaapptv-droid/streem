# =========================================
# RPLAY PRODUCTION BOT
# Ultra Optimized Edition
# FFmpeg 8.1 Ready
# =========================================

import asyncio
import json
import os
import re
import shutil
import time
import logging
from collections import defaultdict

import psutil
from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest, RetryAfter

# =========================================
# CONFIG
# =========================================

with open("settings.json") as f:
    cfg = json.load(f)

TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]

BASE_URL = cfg.get("BASE_URL", "http://YOUR-IP")
PORT = cfg.get("PORT", 8080)

STREAMS_FILE = "streams.json"
HLS_DIR = "/tmp/hls"

os.makedirs(HLS_DIR, exist_ok=True)

# =========================================
# LOGGING
# =========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================================
# DATA
# =========================================

streams = {}
processes = {}

viewers = defaultdict(set)
viewer_last = defaultdict(dict)

last_panels = {}

monitor_running = False
monitor_task = None

# =========================================
# LOAD STREAMS
# =========================================

if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        streams = json.load(f)

for sid, s in streams.items():

    s.setdefault("type", "hls")
    s.setdefault("mode", "copy")

    s.setdefault("fps", "?")

    s.setdefault("logo", "")
    s.setdefault("ua", "ExoPlayerLib/2.18.5")

    s.setdefault("rtmp_server", "")
    s.setdefault("rtmp_key", "")

    s.setdefault("active", False)

    s.setdefault("message_id", None)
    s.setdefault("chat_id", None)

    s.setdefault("start_time", 0)

# =========================================
# SAVE
# =========================================

def save():

    data = {}

    for sid, s in streams.items():

        tmp = s.copy()

        tmp.pop("process", None)

        data[sid] = tmp

    with open(STREAMS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# =========================================
# SYSTEM STATUS
# =========================================

def system_status():

    cpu = psutil.cpu_percent()

    mem = psutil.virtual_memory()

    disk = psutil.disk_usage("/")

    return (
        f"🖥 CPU: {cpu}%\n"
        f"🧠 RAM: {mem.percent}%\n"
        f"💾 DISK: {disk.percent}%\n"
        f"📺 Streams: {len(streams)}"
    )

# =========================================
# HTTP SERVER
# =========================================

async def hls_handler(request):

    sid = request.match_info["name"]

    filename = request.match_info.get("file", "index.m3u8")

    path = os.path.join(HLS_DIR, sid, filename)

    if not os.path.exists(path):
        return web.Response(status=404)

    ip = request.remote

    if ip:
        viewers[sid].add(ip)
        viewer_last[sid][ip] = time.time()

    return web.FileResponse(path)

async def start_http():

    app = web.Application()

    app.router.add_get("/live/{name}/{file:.*}", hls_handler)

    runner = web.AppRunner(app)

    await runner.setup()

    await web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    ).start()

    logging.info(f"HLS HTTP Started :{PORT}")

# =========================================
# TELEGRAM SAFE EDIT
# =========================================

async def safe_edit(bot, chat_id, message_id, text, reply_markup=None):

    try:

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    except BadRequest as e:

        if "Message is not modified" in str(e):
            return

    except RetryAfter as e:

        logging.warning(f"Flood wait {e.retry_after}")

        await asyncio.sleep(e.retry_after)

    except Exception as e:

        logging.error(e)

# =========================================
# PANELS
# =========================================

def panel_text(sid):

    s = streams[sid]

    uptime = "00:00:00"

    if s.get("start_time"):

        uptime = time.strftime(
            "%H:%M:%S",
            time.gmtime(time.time() - s["start_time"])
        )

    viewers_count = len(viewers.get(sid, set()))

    text = (
        f"🎛 *{s['name']}*\n\n"
        f"🟢 الحالة: {'يعمل' if s['active'] else 'متوقف'}\n"
        f"⚙️ الوضع: {'ترميز' if s['mode']=='encode' else 'نسخ'}\n"
        f"🎬 FPS: {s['fps']}\n"
        f"👥 المشاهدين: {viewers_count}\n"
        f"⏱ التشغيل: {uptime}\n\n"
        f"📥 المصدر:\n`{s['source']}`\n"
    )

    if s.get("type", "hls") == "hls":

        text += (
            f"\n🔗\n"
            f"`{BASE_URL}:{PORT}/live/{sid}/index.m3u8`"
        )

    else:

        text += (
            f"\n📡 RTMP\n"
            f"`{s['rtmp_server']}/{s['rtmp_key']}`"
        )

    return text

# =========================================
# KEYBOARD
# =========================================

reply_kb = ReplyKeyboardMarkup([
    ["📺 قائمة HLS", "📡 قائمة RTMP"],
    ["➕ إضافة بث"],
    ["🖥 تشغيل المراقبة", "⛔ إيقاف المراقبة"],
    ["🧹 تنظيف الملفات"]
], resize_keyboard=True)

def stream_panel(sid):

    s = streams[sid]

    kb = []

    if s["active"]:

        kb.append([
            InlineKeyboardButton(
                "⏹ إيقاف",
                callback_data=f"stop_{sid}"
            )
        ])

    else:

        kb.append([
            InlineKeyboardButton(
                "▶ تشغيل",
                callback_data=f"start_{sid}"
            )
        ])

    kb.append([
        InlineKeyboardButton(
            "⚙️ تبديل الوضع",
            callback_data=f"mode_{sid}"
        )
    ])

    kb.append([
        InlineKeyboardButton(
            "🗑 حذف",
            callback_data=f"delete_{sid}"
        )
    ])

    return InlineKeyboardMarkup(kb)

# =========================================
# UPDATE PANEL
# =========================================

async def update_panel(bot, sid):

    s = streams[sid]

    if not s.get("chat_id"):
        return

    text = panel_text(sid)

    if last_panels.get(sid) == text:
        return

    last_panels[sid] = text

    await safe_edit(
        bot,
        s["chat_id"],
        s["message_id"],
        text,
        stream_panel(sid)
    )

# =========================================
# START STREAM
# =========================================

async def start_stream(bot, sid):

    s = streams[sid]

    source = s["source"]

    out_dir = os.path.join(HLS_DIR, sid)

    shutil.rmtree(out_dir, ignore_errors=True)

    os.makedirs(out_dir, exist_ok=True)

    out_file = os.path.join(out_dir, "index.m3u8")

    logo = s.get("logo", "")

    if s["type"] == "hls":

        if s["mode"] == "copy":

            cmd = [
                "ffmpeg",
                "-re",

                "-thread_queue_size", "4096",
                "-analyzeduration", "10000000",
                "-probesize", "10000000",
                "-buffer_size", "8000k",

                "-fflags", "+genpts+discardcorrupt",

                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",

                "-rw_timeout", "10000000",

                "-user_agent", s["ua"],

                "-i", source,

                "-c:v", "copy",

                "-c:a", "aac",
                "-b:a", "128k",

                "-f", "hls",

                "-hls_time", "2",
                "-hls_list_size", "10",

                "-hls_flags", "delete_segments+append_list",

                "-y",
                out_file
            ]

        else:

            if logo:

                vf = (
                    "[1:v]scale=1920:1080[logo];"
                    "[0:v][logo]overlay=0:0"
                )

                cmd = [
                    "ffmpeg",
                    "-re",

                    "-thread_queue_size", "4096",
                    "-analyzeduration", "10000000",
                    "-probesize", "10000000",
                    "-buffer_size", "8000k",

                    "-fflags", "+genpts+discardcorrupt",

                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5",

                    "-rw_timeout", "10000000",

                    "-user_agent", s["ua"],

                    "-i", source,
                    "-i", logo,

                    "-filter_complex", vf,

                    "-c:v", "libx264",

                    "-preset", "ultrafast",
                    "-tune", "zerolatency",

                    "-b:v", "9000k",
                    "-maxrate", "9000k",
                    "-bufsize", "18000k",

                    "-r", "30",

                    "-c:a", "aac",
                    "-b:a", "128k",

                    "-f", "hls",

                    "-hls_time", "2",
                    "-hls_list_size", "10",

                    "-hls_flags", "delete_segments+append_list",

                    "-y",
                    out_file
                ]

            else:

                cmd = [
                    "ffmpeg",
                    "-re",

                    "-thread_queue_size", "4096",
                    "-analyzeduration", "10000000",
                    "-probesize", "10000000",
                    "-buffer_size", "8000k",

                    "-fflags", "+genpts+discardcorrupt",

                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5",

                    "-rw_timeout", "10000000",

                    "-user_agent", s["ua"],

                    "-i", source,

                    "-vf",
                    "scale=1920:1080",

                    "-c:v", "libx264",

                    "-preset", "ultrafast",
                    "-tune", "zerolatency",

                    "-b:v", "9000k",
                    "-maxrate", "9000k",
                    "-bufsize", "18000k",

                    "-r", "30",

                    "-c:a", "aac",
                    "-b:a", "128k",

                    "-f", "hls",

                    "-hls_time", "2",
                    "-hls_list_size", "10",

                    "-hls_flags", "delete_segments+append_list",

                    "-y",
                    out_file
                ]

    else:

        rtmp = f"{s['rtmp_server']}/{s['rtmp_key']}"

        cmd = [
            "ffmpeg",
            "-re",

            "-thread_queue_size", "4096",

            "-i", source,

            "-c:v", "copy",
            "-c:a", "aac",

            "-f", "flv",

            "-y",
            rtmp
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )

    processes[sid] = proc

    s["active"] = True

    s["start_time"] = time.time()

    save()

    await update_panel(bot, sid)

    async def read_logs():

        last_fps_update = 0

        while True:

            line = await proc.stderr.readline()

            if not line:
                break

            txt = line.decode(errors="ignore")

            m = re.search(r"fps=\s*([\d.]+)", txt)

            if m:

                now = time.time()

                if now - last_fps_update >= 10:

                    s["fps"] = m.group(1)

                    last_fps_update = now

                    await update_panel(bot, sid)

    asyncio.create_task(read_logs())

    await proc.wait()

    s["active"] = False

    save()

    await update_panel(bot, sid)

# =========================================
# STOP STREAM
# =========================================

async def stop_stream(bot, sid):

    if sid in processes:

        try:

            processes[sid].terminate()

            await asyncio.sleep(1)

            processes[sid].kill()

        except:
            pass

        processes.pop(sid, None)

    streams[sid]["active"] = False

    save()

    await update_panel(bot, sid)

# =========================================
# MONITOR LOOP
# =========================================

async def monitor_loop(bot, chat_id, message_id):

    global monitor_running

    while monitor_running:

        await safe_edit(
            bot,
            chat_id,
            message_id,
            system_status()
        )

        await asyncio.sleep(5)

# =========================================
# CALLBACKS
# =========================================

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query

    await q.answer()

    data = q.data

    if data.startswith("start_"):

        sid = data.replace("start_", "")

        asyncio.create_task(
            start_stream(context.bot, sid)
        )

    elif data.startswith("stop_"):

        sid = data.replace("stop_", "")

        asyncio.create_task(
            stop_stream(context.bot, sid)
        )

    elif data.startswith("mode_"):

        sid = data.replace("mode_", "")

        s = streams[sid]

        s["mode"] = (
            "encode"
            if s["mode"] == "copy"
            else "copy"
        )

        save()

        await update_panel(context.bot, sid)

    elif data.startswith("delete_"):

        sid = data.replace("delete_", "")

        await stop_stream(context.bot, sid)

        streams.pop(sid)

        save()

        await q.edit_message_text("✅ تم حذف البث")

# =========================================
# HANDLE TEXT
# =========================================

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global monitor_running
    global monitor_task

    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text

    # =====================================
    # MONITOR
    # =====================================

    if text == "🖥 تشغيل المراقبة":

        monitor_running = True

        msg = await update.message.reply_text(
            system_status()
        )

        monitor_task = asyncio.create_task(
            monitor_loop(
                context.bot,
                msg.chat_id,
                msg.message_id
            )
        )

        return

    if text == "⛔ إيقاف المراقبة":

        monitor_running = False

        if monitor_task:
            monitor_task.cancel()

        await update.message.reply_text(
            "⛔ تم إيقاف المراقبة"
        )

        return

    # =====================================
    # CLEAN
    # =====================================

    if text == "🧹 تنظيف الملفات":

        shutil.rmtree(HLS_DIR, ignore_errors=True)

        os.makedirs(HLS_DIR, exist_ok=True)

        await update.message.reply_text(
            "✅ تم تنظيف ملفات HLS"
        )

        return

    # =====================================
    # LISTS
    # =====================================

    if text == "📺 قائمة HLS":

        for sid, s in streams.items():

            if s.get("type") != "hls":
                continue

            msg = await update.message.reply_text(
                panel_text(sid),
                reply_markup=stream_panel(sid),
                parse_mode="Markdown"
            )

            s["chat_id"] = msg.chat_id
            s["message_id"] = msg.message_id

        save()

        return

    if text == "📡 قائمة RTMP":

        for sid, s in streams.items():

            if s.get("type") != "rtmp":
                continue

            msg = await update.message.reply_text(
                panel_text(sid),
                reply_markup=stream_panel(sid),
                parse_mode="Markdown"
            )

            s["chat_id"] = msg.chat_id
            s["message_id"] = msg.message_id

        save()

        return

# =========================================
# START CMD
# =========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🚀 RPLAY PRODUCTION",
        reply_markup=reply_kb
    )

# =========================================
# MAIN
# =========================================

def main():

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    loop.create_task(start_http())

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle
        )
    )

    app.add_handler(
        CallbackQueryHandler(callback)
    )

    logging.info("BOT STARTED")

    app.run_polling()

if __name__ == "__main__":
    main()