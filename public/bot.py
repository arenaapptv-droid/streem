import asyncio
import json
import os
import re
import shutil
import time
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

# =========================================================
# CONFIG
# =========================================================

with open("settings.json") as f:
    cfg = json.load(f)

TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]

BASE_URL = cfg.get("BASE_URL", "http://127.0.0.1")
PORT = cfg.get("PORT", 8080)

HLS_DIR = "/tmp/hls"
STREAMS_FILE = "streams.json"

os.makedirs(HLS_DIR, exist_ok=True)

# =========================================================
# STATE
# =========================================================

streams = {}
processes = {}
viewers = defaultdict(set)
viewer_last = defaultdict(dict)

MAX_ENCODERS = 3
active_encoders = 0
encoder_lock = asyncio.Lock()

last_panel_update = {}

# =========================================================
# LOAD STREAMS
# =========================================================

if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        streams = json.load(f)

# =========================================================
# SAVE
# =========================================================

def save():
    with open(STREAMS_FILE, "w") as f:
        json.dump(streams, f, indent=2)

# =========================================================
# SYSTEM STATUS
# =========================================================

def system_status():
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent

    return (
        f"🖥 CPU: {cpu}%\n"
        f"🧠 RAM: {ram}%\n"
        f"💾 DISK: {disk}%\n"
        f"🎬 Streams: {len(streams)}\n"
        f"🔴 Encoders: {active_encoders}/{MAX_ENCODERS}"
    )

# =========================================================
# CPU GUARD
# =========================================================

def cpu_ok():
    try:
        return psutil.cpu_percent(interval=0.3) < 85
    except:
        return True

# =========================================================
# ENCODER LIMITER
# =========================================================

async def acquire_encoder():
    global active_encoders

    async with encoder_lock:
        if active_encoders >= MAX_ENCODERS:
            return False

        active_encoders += 1
        return True

async def release_encoder():
    global active_encoders

    async with encoder_lock:
        if active_encoders > 0:
            active_encoders -= 1

# =========================================================
# HLS SERVER
# =========================================================

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

# =========================================================
# FFmpeg COMMANDS
# =========================================================

def build_copy_cmd(src, out):
    return [
        "ffmpeg",
        "-loglevel", "error",
        "-re",
        "-fflags", "+genpts+discardcorrupt",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", src,

        "-c", "copy",

        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments+append_list",

        "-y",
        out
    ]

def build_encode_cmd(src, out):
    return [
        "ffmpeg",
        "-loglevel", "error",
        "-re",
        "-i", src,

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",

        "-b:v", "3500k",
        "-maxrate", "3500k",
        "-bufsize", "7000k",

        "-r", "30",
        "-g", "60",

        "-c:a", "aac",
        "-b:a", "128k",

        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments+append_list",

        "-y",
        out
    ]

# =========================================================
# PANEL KEYBOARD
# =========================================================

def panel_keyboard(sid, s):
    active = s.get("active", False)

    kb = []

    if active:
        kb.append([
            InlineKeyboardButton(
                "⏹ إيقاف",
                callback_data=f"stop_{sid}"
            )
        ])
    else:
        kb.append([
            InlineKeyboardButton(
                "▶️ تشغيل",
                callback_data=f"start_{sid}"
            )
        ])

    kb.append([
        InlineKeyboardButton(
            "📥 المصدر",
            callback_data=f"source_{sid}"
        ),

        InlineKeyboardButton(
            "⚙️ الوضع",
            callback_data=f"mode_{sid}"
        )
    ])

    kb.append([
        InlineKeyboardButton(
            "🗑 حذف",
            callback_data=f"delete_{sid}"
        )
    ])

    kb.append([
        InlineKeyboardButton(
            "🔙 الرئيسية",
            callback_data="main_menu"
        )
    ])

    return InlineKeyboardMarkup(kb)

# =========================================================
# PANEL TEXT
# =========================================================

def panel_text(sid):
    s = streams[sid]

    uptime = "00:00:00"

    if s.get("start_time"):
        uptime = time.strftime(
            "%H:%M:%S",
            time.gmtime(time.time() - s["start_time"])
        )

    mode = "COPY" if s["mode"] == "copy" else "ENCODE"

    return (
        f"🎛 {s['name']}\n\n"
        f"📥 Source:\n{s['source']}\n\n"
        f"⚙️ Mode: {mode}\n"
        f"🟢 Active: {s.get('active')}\n"
        f"👥 Viewers: {len(viewers[sid])}\n"
        f"⏱ Uptime: {uptime}\n\n"
        f"🔗 {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    )

# =========================================================
# UPDATE PANEL
# =========================================================

async def update_panel(sid, bot):
    s = streams.get(sid)

    if not s:
        return

    chat_id = s.get("chat_id")
    message_id = s.get("message_id")

    if not chat_id or not message_id:
        return

    now = time.time()

    if now - last_panel_update.get(sid, 0) < 5:
        return

    last_panel_update[sid] = now

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=panel_text(sid),
            reply_markup=panel_keyboard(sid, s)
        )
    except:
        pass

# =========================================================
# START STREAM
# =========================================================

async def start_stream(sid, bot):
    s = streams[sid]

    if s.get("active"):
        return

    if not cpu_ok():
        print("CPU overloaded")
        return

    mode = s.get("mode", "copy")

    if mode == "encode":
        ok = await acquire_encoder()

        if not ok:
            print("Max encoders reached")
            return

    out_dir = os.path.join(HLS_DIR, sid)

    shutil.rmtree(out_dir, ignore_errors=True)

    os.makedirs(out_dir, exist_ok=True)

    out = os.path.join(out_dir, "index.m3u8")

    cmd = (
        build_copy_cmd(s["source"], out)
        if mode == "copy"
        else build_encode_cmd(s["source"], out)
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )

    processes[sid] = proc

    s["active"] = True
    s["start_time"] = time.time()

    save()

    await update_panel(sid, bot)

    await proc.wait()

    processes.pop(sid, None)

    s["active"] = False

    if mode == "encode":
        await release_encoder()

    save()

    await update_panel(sid, bot)

    # auto restart
    await asyncio.sleep(3)

    if s.get("auto_restart", True):
        asyncio.create_task(
            start_stream(sid, bot)
        )

# =========================================================
# STOP STREAM
# =========================================================

async def stop_stream(sid, bot):
    proc = processes.get(sid)

    if proc:
        try:
            proc.terminate()

            await asyncio.sleep(1)

            proc.kill()

            await proc.wait()
        except:
            pass

    processes.pop(sid, None)

    if sid in streams:
        streams[sid]["active"] = False

    save()

    shutil.rmtree(
        os.path.join(HLS_DIR, sid),
        ignore_errors=True
    )

    await update_panel(sid, bot)

# =========================================================
# REPLY KEYBOARD
# =========================================================

reply_kb = ReplyKeyboardMarkup(
    [
        ["📺 قائمة البثوث"],
        ["➕ إضافة بث"],
        ["🖥 مراقبة السيرفر"],
        ["🧹 تنظيف الملفات"]
    ],
    resize_keyboard=True
)

# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(
        "🚀 IPTV Production Bot",
        reply_markup=reply_kb
    )

# =========================================================
# STREAMS LIST
# =========================================================

def streams_keyboard():
    kb = []

    for sid, s in streams.items():
        icon = "🟢" if s.get("active") else "🔴"

        kb.append([
            InlineKeyboardButton(
                f"{icon} {s['name']}",
                callback_data=f"open_{sid}"
            )
        ])

    kb.append([
        InlineKeyboardButton(
            "🔙 الرئيسية",
            callback_data="main_menu"
        )
    ])

    return InlineKeyboardMarkup(kb)

# =========================================================
# HANDLE TEXT
# =========================================================

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text

    if text == "📺 قائمة البثوث":
        await update.message.reply_text(
            "📺 القائمة",
            reply_markup=streams_keyboard()
        )
        return

    if text == "🖥 مراقبة السيرفر":
        await update.message.reply_text(
            system_status()
        )
        return

    if text == "🧹 تنظيف الملفات":
        shutil.rmtree(HLS_DIR, ignore_errors=True)

        os.makedirs(HLS_DIR, exist_ok=True)

        await update.message.reply_text(
            "✅ تم تنظيف الملفات"
        )

        return

    if text == "➕ إضافة بث":
        context.user_data["step"] = "name"

        await update.message.reply_text(
            "أرسل اسم البث"
        )

        return

    # add name
    if context.user_data.get("step") == "name":
        sid = text.replace(" ", "_")

        streams[sid] = {
            "name": text,
            "source": "",
            "mode": "copy",
            "active": False,
            "start_time": 0,
            "auto_restart": True
        }

        context.user_data["sid"] = sid
        context.user_data["step"] = "source"

        save()

        await update.message.reply_text(
            "أرسل المصدر"
        )

        return

    # add source
    if context.user_data.get("step") == "source":
        sid = context.user_data["sid"]

        streams[sid]["source"] = text

        save()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ تم إضافة البث"
        )

        return

# =========================================================
# CALLBACKS
# =========================================================

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    await q.answer()

    data = q.data

    # main menu
    if data == "main_menu":
        await q.edit_message_text(
            "🎛 الرئيسية"
        )
        return

    # open
    if data.startswith("open_"):
        sid = data[5:]

        streams[sid]["chat_id"] = q.message.chat_id
        streams[sid]["message_id"] = q.message.message_id

        save()

        await q.edit_message_text(
            panel_text(sid),
            reply_markup=panel_keyboard(
                sid,
                streams[sid]
            )
        )

        return

    # start
    if data.startswith("start_"):
        sid = data[6:]

        asyncio.create_task(
            start_stream(sid, context.bot)
        )

        await q.answer(
            "🚀 Starting..."
        )

        return

    # stop
    if data.startswith("stop_"):
        sid = data[5:]

        asyncio.create_task(
            stop_stream(sid, context.bot)
        )

        await q.answer(
            "⏹ Stopping..."
        )

        return

    # mode
    if data.startswith("mode_"):
        sid = data[5:]

        old = streams[sid]["mode"]

        streams[sid]["mode"] = (
            "encode"
            if old == "copy"
            else "copy"
        )

        save()

        await update_panel(
            sid,
            context.bot
        )

        return

    # delete
    if data.startswith("delete_"):
        sid = data[7:]

        await stop_stream(
            sid,
            context.bot
        )

        streams.pop(sid, None)

        save()

        await q.edit_message_text(
            "🗑 Deleted"
        )

        return

    # source edit
    if data.startswith("source_"):
        sid = data[7:]

        context.user_data["edit_source"] = sid

        await q.edit_message_text(
            "📥 أرسل المصدر الجديد"
        )

# =========================================================
# MAIN
# =========================================================

async def startup():
    await start_http()

def main():
    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    loop.create_task(startup())

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle
        )
    )

    app.add_handler(
        CallbackQueryHandler(callback)
    )

    print("🚀 BOT RUNNING")

    app.run_polling()

if __name__ == "__main__":
    main()