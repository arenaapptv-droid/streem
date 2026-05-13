import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time
import traceback
from collections import defaultdict

import psutil
from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

logger = logging.getLogger("RplayOptimized")

# =========================================================
# SETTINGS
# =========================================================

with open("settings.json", "r", encoding="utf-8") as f:
    settings = json.load(f)

TOKEN = settings["TOKEN"]
ADMIN_ID = settings["ADMIN_ID"]
BASE_URL = settings.get("BASE_URL", "http://127.0.0.1")
HTTP_PORT = settings.get("HTTP_PORT", 8080)

STREAMS_FILE = "streams_pro.json"
HLS_DIR = "/tmp/hls"

os.makedirs(HLS_DIR, exist_ok=True)

# =========================================================
# MEMORY
# =========================================================

streams = {}
viewer_last_seen = defaultdict(dict)
monitor_tasks = {}
last_panel_text = {}

# =========================================================
# LOAD STREAMS
# =========================================================

if os.path.exists(STREAMS_FILE):
    try:
        with open(STREAMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for sid, s in data.items():
            s.setdefault("name", sid)
            s.setdefault("source", "")
            s.setdefault("logo", "")
            s.setdefault("user_agent", "")
            s.setdefault("active", False)
            s.setdefault("fallback", False)
            s.setdefault("source_online", False)
            s.setdefault("viewers", [])
            s.setdefault("last_fps", "?")
            s.setdefault("uptime", "00:00:00")
            s.setdefault("start_time", 0)
            s.setdefault("panel_msg_id", None)
            s.setdefault("panel_chat_id", None)
            s.setdefault("mode", "copy")
            s.setdefault("rtmp_server", "")
            s.setdefault("rtmp_key", "")
            s.setdefault("type", "hls")
            s.setdefault("process", None)

            s["viewers"] = set(s["viewers"])
            streams[sid] = s
    except Exception as e:
        logger.error(f"Failed loading streams: {e}")

# =========================================================
# SAVE
# =========================================================

def save_streams():
    data = {}
    for sid, s in streams.items():
        copy_data = s.copy()
        if isinstance(copy_data.get("viewers"), set):
            copy_data["viewers"] = list(copy_data["viewers"])
        copy_data.pop("process", None)
        data[sid] = copy_data
    with open(STREAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# =========================================================
# SYSTEM STATUS
# =========================================================

def get_system_status():
    cpu = psutil.cpu_percent(interval=0.2)
    memory = psutil.virtual_memory()
    used_ram = memory.used // (1024 * 1024)
    total_ram = memory.total // (1024 * 1024)
    disk = psutil.disk_usage("/")
    disk_used = disk.used // (1024 * 1024 * 1024)
    disk_total = disk.total // (1024 * 1024 * 1024)
    return (
        f"🖥 CPU: {cpu:.1f}%\n"
        f"🧠 RAM: {used_ram}/{total_ram} MiB\n"
        f"💾 DISK: {disk_used}/{disk_total} GB\n"
        f"📺 Streams: {len(streams)}"
    )

# =========================================================
# VIEWERS
# =========================================================

async def track_viewer(request, stream_name):
    forwarded = request.headers.get("X-Forwarded-For")
    ip = forwarded.split(",")[0].strip() if forwarded else request.remote
    now = time.time()
    if stream_name in streams:
        s = streams[stream_name]
        if s.get("type") != "hls":
            return
        if not isinstance(s.get("viewers"), set):
            s["viewers"] = set()
        s["viewers"].add(ip)
        viewer_last_seen[stream_name][ip] = now

def clean_viewers():
    now = time.time()
    for sid, s in streams.items():
        if s.get("type") != "hls":
            continue
        if not isinstance(s.get("viewers"), set):
            s["viewers"] = set()
        for ip in list(s["viewers"]):
            if now - viewer_last_seen[sid].get(ip, 0) > 15:
                s["viewers"].discard(ip)
                viewer_last_seen[sid].pop(ip, None)

# =========================================================
# HTTP SERVER
# =========================================================

async def handle_hls(request):
    name = request.match_info["name"]
    file = request.match_info.get("file", "index.m3u8")
    path = os.path.join(HLS_DIR, name, file)
    if not os.path.exists(path):
        return web.Response(status=404)
    await track_viewer(request, name)
    if path.endswith(".m3u8"):
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "application/vnd.apple.mpegurl",
                "Cache-Control": "no-cache",
            }
        )
    if path.endswith(".ts"):
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "video/mp2t",
                "Cache-Control": "no-cache",
            }
        )
    return web.FileResponse(path)

async def start_http_server():
    app = web.Application()
    app.router.add_get("/live/{name}/{file:.*}", handle_hls)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    logger.warning(f"HTTP running on {HTTP_PORT}")

# =========================================================
# TELEGRAM UI
# =========================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📺 HLS"), KeyboardButton("📡 RTMP")],
        [KeyboardButton("➕ إضافة"), KeyboardButton("🖥 مراقبة")],
    ],
    resize_keyboard=True,
)

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 بثوث HLS", callback_data="list_hls")],
        [InlineKeyboardButton("📡 بثوث RTMP", callback_data="list_rtmp")],
        [InlineKeyboardButton("➕ إضافة بث", callback_data="add_stream")],
        [InlineKeyboardButton("🖥 مراقبة السيرفر", callback_data="monitor")],
    ])

def navigation_row():
    return [
        InlineKeyboardButton("HLS", callback_data="list_hls"),
        InlineKeyboardButton("RTMP", callback_data="list_rtmp"),
        InlineKeyboardButton("➕", callback_data="add_stream"),
        InlineKeyboardButton("🖥", callback_data="monitor"),
    ]

def stream_list(stream_type):
    kb = []
    for sid, s in streams.items():
        if s.get("type") != stream_type:
            continue
        status = "🟢" if s.get("active") else "⏹"
        kb.append([
            InlineKeyboardButton(
                f"{status} {s['name']}",
                callback_data=f"panel_{sid}"
            )
        ])
    kb.append(navigation_row())
    return InlineKeyboardMarkup(kb)

def stream_panel_keyboard(sid, s):
    mode = s.get("mode", "copy")
    kb = []
    if s.get("active"):
        kb.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{sid}")])
    else:
        kb.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{sid}")])
    kb.append([
        InlineKeyboardButton("📥 مصدر", callback_data=f"source_{sid}"),
        InlineKeyboardButton("🖼 شعار", callback_data=f"logo_{sid}"),
    ])
    kb.append([
        InlineKeyboardButton("🕵️ UA", callback_data=f"ua_{sid}"),
        InlineKeyboardButton("✏️ اسم", callback_data=f"rename_{sid}"),
    ])
    if s.get("type") == "rtmp":
        kb.append([
            InlineKeyboardButton("📡 سيرفر", callback_data=f"rtmpsrv_{sid}"),
            InlineKeyboardButton("🔑 مفتاح", callback_data=f"rtmpkey_{sid}"),
        ])
    toggle_text = "⚡ Copy" if mode == "transcode" else "🎞 Transcode"
    kb.append([InlineKeyboardButton(toggle_text, callback_data=f"togglemode_{sid}")])
    kb.append([InlineKeyboardButton("🗑 حذف", callback_data=f"delete_{sid}")])
    if s.get("active"):
        viewers = len(s.get("viewers", set()))
        fps = s.get("last_fps", "?")
        uptime = s.get("uptime", "00:00:00")
        mode_label = "COPY" if mode == "copy" else "TRANSCODE"
        kb.append([
            InlineKeyboardButton(
                f"FPS:{fps} | 👥{viewers} | {uptime} | {mode_label}",
                callback_data="noop"
            )
        ])
    kb.append(navigation_row())
    return InlineKeyboardMarkup(kb)

# =========================================================
# HELPERS
# =========================================================

async def check_admin(update):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        if update.message:
            await update.message.reply_text("🚫 غير مصرح")
        elif update.callback_query:
            await update.callback_query.answer("🚫 غير مصرح", show_alert=True)
        return False
    return True

# =========================================================
# START
# =========================================================

async def start(update, context):
    if not await check_admin(update):
        return
    await update.message.reply_text("🖥 Rplay Optimized", reply_markup=main_menu())
    await update.message.reply_text("اختر من الأزرار السفلية", reply_markup=MAIN_KEYBOARD)

# =========================================================
# MONITOR
# =========================================================

async def start_monitor_live(query, chat_id):
    if chat_id in monitor_tasks:
        monitor_tasks[chat_id].cancel()

    async def loop_monitor():
        try:
            while True:
                status = get_system_status()
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏹ إيقاف", callback_data="stop_monitor")],
                    [InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")],
                ])
                try:
                    await query.edit_message_text(status, reply_markup=kb)
                except:
                    pass
                await asyncio.sleep(20)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(loop_monitor())
    monitor_tasks[chat_id] = task

async def stop_monitor(chat_id, query):
    if chat_id in monitor_tasks:
        monitor_tasks[chat_id].cancel()
        del monitor_tasks[chat_id]
    await query.edit_message_text(get_system_status(), reply_markup=main_menu())

# =========================================================
# PANEL UPDATE
# =========================================================

async def update_panel_message(sid, bot):
    s = streams.get(sid)
    if not s or not s.get("panel_msg_id"):
        return
    try:
        name = s.get("name", sid)
        stream_type = s.get("type", "hls")
        text = (
            f"🎛️ {name} ({stream_type.upper()})\n"
            f"📥 المصدر: {s.get('source') or 'غير محدد'}\n"
            f"🖼 الشعار: {'موجود' if s.get('logo') else 'لا يوجد'}\n"
            f"🕵️ UA: {s.get('user_agent') or 'افتراضي'}"
        )
        if stream_type == "hls":
            text += f"\n🔗 {BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8"
        if stream_type == "rtmp":
            text += f"\n📡 {s.get('rtmp_server', '')}/{s.get('rtmp_key', '')}"
        if last_panel_text.get(sid) == text:
            return
        last_panel_text[sid] = text
        await bot.edit_message_text(
            chat_id=s["panel_chat_id"],
            message_id=s["panel_msg_id"],
            text=text,
            reply_markup=stream_panel_keyboard(sid, s)
        )
    except Exception as e:
        logger.error(f"Panel update error: {e}")

# =========================================================
# FFMPEG COMMANDS
# =========================================================

def build_hls_copy(src, ua, output):
    return [
        "ffmpeg", "-loglevel", "warning", "-re",
        "-user_agent", ua,
        "-thread_queue_size", "4096",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-i", src,
        "-map", "0:v?", "-map", "0:a?",
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "hls",
        "-hls_time", "4",
        "-hls_list_size", "6",
        "-hls_delete_threshold", "2",
        "-hls_flags", "delete_segments+append_list",
        output
    ]

def build_hls_transcode(src, ua, logo, output):
    cmd = [
        "ffmpeg", "-loglevel", "warning", "-re",
        "-user_agent", ua,
        "-thread_queue_size", "4096",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-i", src,
    ]
    if logo:
        cmd += [
            "-i", logo,
            "-filter_complex",
            "[1:v]scale=120:-1[logo];[0:v][logo]overlay=W-w-15:H-h-15"
        ]
    cmd += [
        "-map", "0:v?", "-map", "0:a?",
        "-threads", "2",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-crf", "25",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        "-f", "hls",
        "-hls_time", "4",
        "-hls_list_size", "6",
        "-hls_delete_threshold", "2",
        "-hls_flags", "delete_segments+append_list",
        output
    ]
    return cmd

def build_rtmp_copy(src, ua, rtmp_url):
    return [
        "ffmpeg", "-loglevel", "warning", "-re",
        "-user_agent", ua,
        "-thread_queue_size", "4096",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-i", src,
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "flv",
        rtmp_url
    ]

def build_rtmp_transcode(src, ua, logo, rtmp_url):
    cmd = [
        "ffmpeg", "-loglevel", "warning", "-re",
        "-user_agent", ua,
        "-thread_queue_size", "4096",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-i", src,
    ]
    if logo:
        cmd += [
            "-i", logo,
            "-filter_complex",
            "[1:v]scale=120:-1[logo];[0:v][logo]overlay=W-w-15:H-h-15"
        ]
    cmd += [
        "-threads", "2",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-crf", "25",
        "-c:a", "aac",
        "-b:a", "128k",
        "-f", "flv",
        rtmp_url
    ]
    return cmd

# =========================================================
# STREAM CONTROL
# =========================================================

async def start_stream(sid, bot):
    s = streams[sid]
    src = s["source"]
    logo = s.get("logo", "")
    ua = s.get("user_agent") or "ExoPlayerLib/2.18.5"
    mode = s.get("mode", "copy")
    stream_type = s.get("type", "hls")

    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_playlist = os.path.join(out_dir, "index.m3u8")

    if stream_type == "hls":
        if mode == "copy" or not logo:
            cmd = build_hls_copy(src, ua, out_playlist)
        else:
            cmd = build_hls_transcode(src, ua, logo, out_playlist)
    else:
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        if mode == "copy" or not logo:
            cmd = build_rtmp_copy(src, ua, rtmp_url)
        else:
            cmd = build_rtmp_transcode(src, ua, logo, rtmp_url)

    s["active"] = True
    s["start_time"] = time.time()
    save_streams()

    retries = 3
    while s["active"]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        s["process"] = proc
        s["source_online"] = True

        async def stderr_reader():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                decoded = line.decode(errors="ignore")
                fps_match = re.search(r"fps=\s*([\d.]+)", decoded)
                if fps_match:
                    s["last_fps"] = fps_match.group(1)

        asyncio.create_task(stderr_reader())

        while proc.returncode is None:
            clean_viewers()
            s["uptime"] = time.strftime(
                "%H:%M:%S",
                time.gmtime(time.time() - s["start_time"])
            )
            await update_panel_message(sid, bot)
            await asyncio.sleep(20)

        await proc.wait()
        s["source_online"] = False
        await update_panel_message(sid, bot)

        retries -= 1
        if retries <= 0:
            break
        await asyncio.sleep(3)

    s["active"] = False
    save_streams()

# =========================================================
# STOP STREAM
# =========================================================

async def stop_stream(sid, bot):
    s = streams.get(sid)
    if not s:
        return
    proc = s.get("process")
    if proc:
        try:
            proc.send_signal(signal.SIGTERM)
            await asyncio.sleep(1)
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
        except:
            pass
    s["process"] = None
    s["active"] = False
    s["source_online"] = False
    s["fallback"] = False
    s["last_fps"] = "?"
    s["uptime"] = "00:00:00"
    s["start_time"] = 0
    if isinstance(s.get("viewers"), set):
        s["viewers"].clear()
    stream_dir = os.path.join(HLS_DIR, sid)
    if os.path.exists(stream_dir):
        try:
            shutil.rmtree(stream_dir)
        except:
            pass
    save_streams()
    await update_panel_message(sid, bot)

# =========================================================
# CALLBACKS
# =========================================================

async def button_handler(update, context):
    q = update.callback_query
    await q.answer()
    if not await check_admin(update):
        return
    data = q.data

    if data == "main_menu":
        await q.edit_message_text("🖥 Rplay Optimized", reply_markup=main_menu())
        return
    if data == "monitor":
        await start_monitor_live(q, q.message.chat_id)
        return
    if data == "stop_monitor":
        await stop_monitor(q.message.chat_id, q)
        return
    if data in ("list_hls", "list_rtmp"):
        stream_type = "hls" if data == "list_hls" else "rtmp"
        await q.edit_message_text(
            f"📋 {stream_type.upper()} Streams",
            reply_markup=stream_list(stream_type)
        )
        return
    if data == "add_stream":
        context.user_data["mode"] = "add_stream_name"
        await q.edit_message_text("📝 أرسل اسم البث:")
        return
    if data.startswith("panel_"):
        sid = data.split("_", 1)[1]
        s = streams.get(sid)
        if not s:
            return
        s["panel_msg_id"] = q.message.message_id
        s["panel_chat_id"] = q.message.chat_id
        await update_panel_message(sid, context.bot)
        return

    # باقي الأزرار: start, stop, source, logo, ua, rename, rtmpsrv, rtmpkey, togglemode, delete
    # ستتم إضافتها بعد ذلك

# =========================================================
# MESSAGES
# =========================================================

async def msg_handler(update, context):
    if not await check_admin(update):
        return
    text = update.message.text.strip()

    if text == "📺 HLS":
        await update.message.reply_text("📋 HLS Streams", reply_markup=stream_list("hls"))
        return
    if text == "📡 RTMP":
        await update.message.reply_text("📋 RTMP Streams", reply_markup=stream_list("rtmp"))
        return
    if text == "🖥 مراقبة":
        msg = await update.message.reply_text("⏳ جاري التحميل...")
        class FakeQuery:
            async def edit_message_text(self, *args, **kwargs):
                return await context.bot.edit_message_text(
                    chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    *args,
                    **kwargs
                )
        await start_monitor_live(FakeQuery(), msg.chat_id)
        return

    # ========== إضافة بث جديد ==========
    if context.user_data.get("mode") == "add_stream_name":
        name = text.strip()
        sid = name.replace(" ", "_") + str(int(time.time()))

        streams[sid] = {
            "name": name,
            "source": "",
            "logo": "",
            "user_agent": "",
            "active": False,
            "fallback": False,
            "source_online": False,
            "viewers": set(),
            "last_fps": "?",
            "uptime": "00:00:00",
            "start_time": 0,
            "panel_msg_id": None,
            "panel_chat_id": None,
            "mode": "copy",
            "rtmp_server": "",
            "rtmp_key": "",
            "type": "hls",
            "process": None,
        }

        save_streams()
        context.user_data["mode"] = None
        await update.message.reply_text(f"✅ تم إضافة البث: {name}")
        return

    # ========== معالجة التعديلات ==========
    # سيتم إكمالها حسب الحاجة (source, logo, ua, rename, rtmp_server, rtmp_key)
    # لكن يمكن إضافتها لاحقاً

# =========================================================
# ERRORS
# =========================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tb = ''.join(traceback.format_exception(
        None, context.error, context.error.__traceback__
    ))
    logger.error(tb)

# =========================================================
# MAIN
# =========================================================

async def startup():
    await start_http_server()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(startup())

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    app.add_error_handler(error_handler)

    logger.warning("Rplay Optimized Ready")
    app.run_polling(drop_pending_updates=True)