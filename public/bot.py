# =========================================================
# IPTV PRODUCTION BOT
# HLS + RTMP
# FULL FRAME LOGO
# PREBUFFER
# FAST UI
# SERVER MONITOR
# =========================================================

import asyncio
import json
import os
import shutil
import time
from collections import defaultdict

import psutil
from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
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

monitor_tasks = {}

MAX_ENCODERS = 3
active_encoders = 0

# =========================================================
# LOAD
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
        f"⚙️ Encoders: {active_encoders}/{MAX_ENCODERS}"
    )

# =========================================================
# HLS SERVER
# =========================================================

async def hls_handler(request):

    sid = request.match_info["name"]

    filename = request.match_info.get(
        "file",
        "index.m3u8"
    )

    path = os.path.join(
        HLS_DIR,
        sid,
        filename
    )

    if not os.path.exists(path):
        return web.Response(status=404)

    ip = request.remote

    if ip:
        viewers[sid].add(ip)
        viewer_last[sid][ip] = time.time()

    return web.FileResponse(path)

async def start_http():

    app = web.Application()

    app.router.add_get(
        "/live/{name}/{file:.*}",
        hls_handler
    )

    runner = web.AppRunner(app)

    await runner.setup()

    await web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    ).start()

# =========================================================
# COPY CMD
# =========================================================

def build_copy_cmd(src, out):

    return [

        "ffmpeg",

        "-hide_banner",

        "-loglevel", "warning",

        "-thread_queue_size", "4096",

        "-fflags", "+genpts+discardcorrupt+nobuffer",

        "-flags", "low_delay",

        "-rw_timeout", "15000000",

        "-analyzeduration", "50M",

        "-probesize", "50M",

        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",

        "-i", src,

        "-map", "0:v:0?",
        "-map", "0:a:0?",

        "-c:v", "copy",

        "-c:a", "aac",

        "-b:a", "128k",

        "-ar", "44100",

        "-ac", "2",

        "-max_muxing_queue_size", "4096",

        "-f", "hls",

        "-hls_time", "2",

        "-hls_list_size", "10",

        "-hls_delete_threshold", "2",

        "-hls_flags",
        "delete_segments+append_list+independent_segments+temp_file",

        "-hls_segment_type", "mpegts",

        "-hls_allow_cache", "1",

        "-start_number", "1",

        "-y",
        out
    ]

# =========================================================
# ENCODE CMD
# =========================================================

def build_encode_cmd(src, out, logo=None):

    cmd = [

        "ffmpeg",

        "-hide_banner",

        "-loglevel", "warning",

        "-thread_queue_size", "4096",

        "-fflags", "+genpts+discardcorrupt+nobuffer",

        "-flags", "low_delay",

        "-max_delay", "500000",

        "-analyzeduration", "100M",

        "-probesize", "100M",

        "-rw_timeout", "15000000",

        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",

        "-i", src,
    ]

    # =====================================================
    # FULL SCREEN LOGO
    # =====================================================

    if logo:

        cmd += [

            "-loop", "1",

            "-i", logo,

            "-filter_complex",

            (
                "[0:v]"
                "fps=30,"
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
                "[base];"

                "[1:v]"
                "scale=1920:1080,"
                "format=rgba,"
                "colorchannelmixer=aa=1"
                "[logo];"

                "[base][logo]"
                "overlay=0:0"
            )
        ]

    else:

        cmd += [

            "-vf",

            (
                "fps=30,"
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
            )
        ]

    cmd += [

        "-map", "0:v:0?",
        "-map", "0:a:0?",

        "-c:v", "libx264",

        "-preset", "veryfast",

        "-tune", "zerolatency",

        "-pix_fmt", "yuv420p",

        "-r", "30",

        "-g", "60",

        "-keyint_min", "60",

        "-sc_threshold", "0",

        "-b:v", "4500k",

        "-maxrate", "4500k",

        "-bufsize", "9000k",

        "-c:a", "aac",

        "-b:a", "128k",

        "-ar", "44100",

        "-ac", "2",

        "-muxdelay", "0",

        "-muxpreload", "0",

        "-max_muxing_queue_size", "4096",

        "-f", "hls",

        "-hls_time", "2",

        "-hls_list_size", "10",

        "-hls_delete_threshold", "2",

        "-hls_flags",
        "delete_segments+append_list+independent_segments+temp_file",

        "-hls_segment_type", "mpegts",

        "-hls_allow_cache", "1",

        "-start_number", "1",

        "-y",
        out
    ]

    return cmd

# =========================================================
# RTMP CMD
# =========================================================

def build_rtmp_cmd(src, rtmp_url, logo=None):

    cmd = [

        "ffmpeg",

        "-hide_banner",

        "-loglevel", "warning",

        "-thread_queue_size", "4096",

        "-fflags", "+genpts+discardcorrupt+nobuffer",

        "-flags", "low_delay",

        "-rw_timeout", "15000000",

        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",

        "-i", src
    ]

    if logo:

        cmd += [

            "-loop", "1",

            "-i", logo,

            "-filter_complex",

            (
                "[0:v]"
                "fps=30,"
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
                "[base];"

                "[1:v]"
                "scale=1920:1080"
                "[logo];"

                "[base][logo]"
                "overlay=0:0"
            )
        ]

    cmd += [

        "-map", "0:v:0?",
        "-map", "0:a:0?",

        "-c:v", "libx264",

        "-preset", "veryfast",

        "-tune", "zerolatency",

        "-b:v", "4500k",

        "-maxrate", "4500k",

        "-bufsize", "9000k",

        "-r", "30",

        "-g", "60",

        "-pix_fmt", "yuv420p",

        "-c:a", "aac",

        "-b:a", "128k",

        "-ar", "44100",

        "-ac", "2",

        "-f", "flv",

        rtmp_url
    ]

    return cmd

# =========================================================
# KEYBOARDS
# =========================================================

reply_kb = ReplyKeyboardMarkup(

    [
        ["📺 قائمة البثوث"],

        ["➕ إضافة بث"],

        ["📡 إضافة RTMP"],

        ["🖥 مراقبة السيرفر"],

        ["🧹 تنظيف الملفات"]
    ],

    resize_keyboard=True
)

# =========================================================
# STREAMS KEYBOARD
# =========================================================

def streams_keyboard():

    kb = []

    for sid, s in streams.items():

        icon = (
            "🟢"
            if s.get("active")
            else "🔴"
        )

        kb.append([
            InlineKeyboardButton(
                f"{icon} {s['name']}",
                callback_data=f"open_{sid}"
            )
        ])

    return InlineKeyboardMarkup(kb)

# =========================================================
# PANEL
# =========================================================

def panel_keyboard(sid):

    return InlineKeyboardMarkup([

        [

            InlineKeyboardButton(
                "▶️ تشغيل",
                callback_data=f"start_{sid}"
            ),

            InlineKeyboardButton(
                "⏹ إيقاف",
                callback_data=f"stop_{sid}"
            )
        ],

        [

            InlineKeyboardButton(
                "📥 المصدر",
                callback_data=f"source_{sid}"
            ),

            InlineKeyboardButton(
                "🖼 الشعار",
                callback_data=f"logo_{sid}"
            )
        ],

        [

            InlineKeyboardButton(
                "⚙️ الوضع",
                callback_data=f"mode_{sid}"
            ),

            InlineKeyboardButton(
                "🗑 حذف",
                callback_data=f"delete_{sid}"
            )
        ]
    ])

# =========================================================
# PANEL TEXT
# =========================================================

def panel_text(sid):

    s = streams[sid]

    uptime = "00:00:00"

    if s.get("start_time"):

        uptime = time.strftime(
            "%H:%M:%S",
            time.gmtime(
                time.time() - s["start_time"]
            )
        )

    text = (

        f"🎛 {s['name']}\n\n"

        f"📥 {s['source']}\n\n"

        f"⚙️ {s['mode']}\n"

        f"🟢 {s.get('active')}\n"

        f"👥 {len(viewers[sid])}\n"

        f"⏱ {uptime}\n"
    )

    if s["type"] == "hls":

        text += (
            f"\n🔗 "
            f"{BASE_URL}:{PORT}/live/{sid}/index.m3u8"
        )

    else:

        text += (
            f"\n📡 "
            f"{s['rtmp_url']}"
        )

    return text

# =========================================================
# UPDATE PANEL
# =========================================================

async def update_panel(sid, bot):

    s = streams[sid]

    if not s.get("chat_id"):
        return

    try:

        await bot.edit_message_text(

            chat_id=s["chat_id"],

            message_id=s["message_id"],

            text=panel_text(sid),

            reply_markup=panel_keyboard(sid)
        )

    except:
        pass

# =========================================================
# START STREAM
# =========================================================

async def start_stream(sid, bot):

    global active_encoders

    s = streams[sid]

    if s.get("active"):
        return

    out_dir = os.path.join(HLS_DIR, sid)

    shutil.rmtree(out_dir, ignore_errors=True)

    os.makedirs(out_dir, exist_ok=True)

    out = os.path.join(out_dir, "index.m3u8")

    mode = s.get("mode", "copy")

    logo = s.get("logo")

    if s["type"] == "hls":

        if mode == "copy":

            cmd = build_copy_cmd(
                s["source"],
                out
            )

        else:

            if active_encoders >= MAX_ENCODERS:
                print("MAX ENCODERS")
                return

            active_encoders += 1

            cmd = build_encode_cmd(
                s["source"],
                out,
                logo
            )

    else:

        if active_encoders >= MAX_ENCODERS:
            print("MAX ENCODERS")
            return

        active_encoders += 1

        cmd = build_rtmp_cmd(
            s["source"],
            s["rtmp_url"],
            logo
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

    s["active"] = False

    processes.pop(sid, None)

    if mode == "encode" or s["type"] == "rtmp":
        active_encoders -= 1

    save()

    await update_panel(sid, bot)

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

        except:
            pass

    processes.pop(sid, None)

    streams[sid]["active"] = False

    save()

    await update_panel(sid, bot)

# =========================================================
# MONITOR LOOP
# =========================================================

async def monitor_loop(bot, chat_id, msg_id):

    while True:

        try:

            kb = InlineKeyboardMarkup([

                [

                    InlineKeyboardButton(
                        "⏹ إيقاف المراقبة",
                        callback_data="stop_monitor"
                    )
                ]
            ])

            await bot.edit_message_text(

                chat_id=chat_id,

                message_id=msg_id,

                text=system_status(),

                reply_markup=kb
            )

        except:
            pass

        await asyncio.sleep(1)

# =========================================================
# START CMD
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(

        "🚀 IPTV PRODUCTION BOT",

        reply_markup=reply_kb
    )

# =========================================================
# HANDLE TEXT
# =========================================================

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text

    # =====================================================
    # LIST
    # =====================================================

    if text == "📺 قائمة البثوث":

        await update.message.reply_text(

            "📺 القائمة",

            reply_markup=streams_keyboard()
        )

        return

    # =====================================================
    # MONITOR
    # =====================================================

    if text == "🖥 مراقبة السيرفر":

        kb = InlineKeyboardMarkup([

            [

                InlineKeyboardButton(
                    "⏹ إيقاف المراقبة",
                    callback_data="stop_monitor"
                )
            ]
        ])

        msg = await update.message.reply_text(

            system_status(),

            reply_markup=kb
        )

        task = asyncio.create_task(

            monitor_loop(
                context.bot,
                msg.chat_id,
                msg.message_id
            )
        )

        monitor_tasks[msg.message_id] = task

        return

    # =====================================================
    # CLEAN
    # =====================================================

    if text == "🧹 تنظيف الملفات":

        shutil.rmtree(
            HLS_DIR,
            ignore_errors=True
        )

        os.makedirs(HLS_DIR, exist_ok=True)

        await update.message.reply_text(
            "✅ تم تنظيف الملفات"
        )

        return

    # =====================================================
    # ADD HLS
    # =====================================================

    if text == "➕ إضافة بث":

        context.user_data["step"] = "add_name"

        context.user_data["type"] = "hls"

        await update.message.reply_text(
            "أرسل اسم البث"
        )

        return

    # =====================================================
    # ADD RTMP
    # =====================================================

    if text == "📡 إضافة RTMP":

        context.user_data["step"] = "add_name"

        context.user_data["type"] = "rtmp"

        await update.message.reply_text(
            "أرسل اسم بث RTMP"
        )

        return

    # =====================================================
    # NAME
    # =====================================================

    if context.user_data.get("step") == "add_name":

        sid = text.replace(" ", "_")

        streams[sid] = {

            "name": text,

            "source": "",

            "logo": "",

            "mode": "copy",

            "type": context.user_data["type"],

            "rtmp_url": "",

            "active": False,

            "start_time": 0
        }

        context.user_data["sid"] = sid

        context.user_data["step"] = "add_source"

        save()

        await update.message.reply_text(
            "أرسل المصدر"
        )

        return

    # =====================================================
    # SOURCE
    # =====================================================

    if context.user_data.get("step") == "add_source":

        sid = context.user_data["sid"]

        streams[sid]["source"] = text

        if streams[sid]["type"] == "rtmp":

            context.user_data["step"] = "add_rtmp"

            save()

            await update.message.reply_text(
                "أرسل رابط RTMP"
            )

            return

        save()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ تم إضافة البث"
        )

        return

    # =====================================================
    # RTMP URL
    # =====================================================

    if context.user_data.get("step") == "add_rtmp":

        sid = context.user_data["sid"]

        streams[sid]["rtmp_url"] = text

        save()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ تم إضافة RTMP"
        )

# =========================================================
# CALLBACKS
# =========================================================

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query

    await q.answer()

    data = q.data

    # =====================================================
    # STOP MONITOR
    # =====================================================

    if data == "stop_monitor":

        task = monitor_tasks.get(
            q.message.message_id
        )

        if task:
            task.cancel()

        await q.edit_message_text(
            "⏹ تم إيقاف المراقبة"
        )

        return

    # =====================================================
    # OPEN
    # =====================================================

    if data.startswith("open_"):

        sid = data[5:]

        streams[sid]["chat_id"] = q.message.chat_id

        streams[sid]["message_id"] = q.message.message_id

        save()

        await q.edit_message_text(

            panel_text(sid),

            reply_markup=panel_keyboard(sid)
        )

        return

    # =====================================================
    # START
    # =====================================================

    if data.startswith("start_"):

        sid = data[6:]

        asyncio.create_task(
            start_stream(
                sid,
                context.bot
            )
        )

        return

    # =====================================================
    # STOP
    # =====================================================

    if data.startswith("stop_"):

        sid = data[5:]

        asyncio.create_task(
            stop_stream(
                sid,
                context.bot
            )
        )

        return

    # =====================================================
    # MODE
    # =====================================================

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

    # =====================================================
    # DELETE
    # =====================================================

    if data.startswith("delete_"):

        sid = data[7:]

        await stop_stream(
            sid,
            context.bot
        )

        streams.pop(sid, None)

        save()

        await q.edit_message_text(
            "🗑 تم الحذف"
        )

        return

    # =====================================================
    # SOURCE
    # =====================================================

    if data.startswith("source_"):

        sid = data[7:]

        context.user_data["edit_source"] = sid

        await q.edit_message_text(
            "أرسل المصدر الجديد"
        )

        return

    # =====================================================
    # LOGO
    # =====================================================

    if data.startswith("logo_"):

        sid = data[5:]

        context.user_data["edit_logo"] = sid

        await q.edit_message_text(
            "أرسل رابط الشعار"
        )

        return

# =========================================================
# STARTUP
# =========================================================

async def startup():
    await start_http()

# =========================================================
# MAIN
# =========================================================

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