import asyncio
import json
import os
import re
import shutil
import time
import signal
from collections import defaultdict

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import BadRequest

# =========================================
# PSUTIL
# =========================================
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
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

# إعدادات الترميز الموفرة
VIDEO_BITRATE = cfg.get("VIDEO_BITRATE", "4000k")
AUDIO_BITRATE = cfg.get("AUDIO_BITRATE", "128k")
PRESET = cfg.get("PRESET", "ultrafast")
CRF = cfg.get("CRF", 28)

HLS_DIR = "/tmp/hls"
STREAMS_FILE = "streams.json"

os.makedirs(HLS_DIR, exist_ok=True)

streams = {}
processes = {}
viewers = defaultdict(set)
viewer_last = defaultdict(dict)

monitor_active = False
monitor_task = None

if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        streams = json.load(f)
    for sid, s in streams.items():
        if "viewers" in s and isinstance(s["viewers"], list):
            s["viewers"] = set(s["viewers"])
        s.setdefault("fps", "?")
        s.setdefault("logo", "")
        s.setdefault("ua", "ExoPlayerLib/2.18.5")
        s.setdefault("rtmp_server", "")
        s.setdefault("rtmp_key", "")
        s.setdefault("type", "hls")
        s.setdefault("mode", "copy")
        s.setdefault("message_id", None)
        s.setdefault("chat_id", None)
        s.setdefault("start_time", 0)

def save_streams():
    data = {}
    for sid, s in streams.items():
        tmp = s.copy()
        if "viewers" in tmp and isinstance(tmp["viewers"], set):
            tmp["viewers"] = list(tmp["viewers"])
        tmp.pop("process", None)
        data[sid] = tmp
    with open(STREAMS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# =========================================
# HTTP SERVER (HLS)
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
    print(f"✅ HTTP on port {PORT}")

# =========================================
# SYSTEM STATUS
# =========================================
def system_status():
    if PSUTIL_AVAILABLE:
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return (
            f"🖥️ CPU: {cpu}%\n"
            f"🧠 RAM: {mem.percent}% ({mem.used//(1024**2)}/{mem.total//(1024**2)} MB)\n"
            f"💾 Disk: {disk.percent}% ({disk.used//(1024**3)}/{disk.total//(1024**3)} GB)\n"
            f"📺 Streams: {len(streams)}"
        )
    else:
        load = "N/A"
        try:
            with open("/proc/loadavg") as f:
                load = f.read().split()[0]
        except: pass
        return f"🖥️ Load: {load}\n📺 Streams: {len(streams)}"

# =========================================
# STREAM PANEL
# =========================================
def get_panel_text(sid):
    s = streams.get(sid)
    if not s:
        return "❌ Stream not found"
    uptime = "00:00:00"
    if s.get("start_time"):
        uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
    viewers_count = len(viewers.get(sid, set()))
    text = (
        f"🎛️ **{s['name']}**\n"
        f"📥 Source: `{s['source']}`\n"
        f"🖼 Logo: {'✅' if s.get('logo') else '❌'}\n"
        f"🕵️ UA: `{s.get('ua')}`\n"
        f"⚙️ Mode: {'Copy' if s['mode']=='copy' else 'Transcode'}\n"
        f"🟢 Status: {'Running' if s.get('active') else 'Stopped'}\n"
        f"🎬 FPS: {s.get('fps','?')}\n"
        f"👥 Viewers: {viewers_count}\n"
        f"⏱️ Uptime: {uptime}\n"
    )
    if s["type"] == "hls":
        text += f"\n🔗 HLS: {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    else:
        text += f"\n📡 RTMP: {s.get('rtmp_server')}/{s.get('rtmp_key')}"
    return text

def stream_panel_keyboard(sid, s):
    active = s.get("active", False)
    mode = s.get("mode", "copy")
    typ = s.get("type", "hls")
    kb = []
    if active:
        kb.append([InlineKeyboardButton("⏹ Stop", callback_data=f"stop_{sid}")])
    else:
        kb.append([InlineKeyboardButton("▶️ Start", callback_data=f"start_{sid}")])
    kb.append([
        InlineKeyboardButton("📥 Source", callback_data=f"source_{sid}"),
        InlineKeyboardButton("🖼 Logo", callback_data=f"logo_{sid}")
    ])
    kb.append([
        InlineKeyboardButton("🕵️ UA", callback_data=f"ua_{sid}"),
        InlineKeyboardButton("✏️ Rename", callback_data=f"rename_{sid}")
    ])
    if typ == "rtmp":
        kb.append([
            InlineKeyboardButton("📡 RTMP Server", callback_data=f"rtmpsrv_{sid}"),
            InlineKeyboardButton("🔑 RTMP Key", callback_data=f"rtmpkey_{sid}")
        ])
    toggle_text = "🔄 Copy" if mode == "encode" else "⚙️ Transcode"
    kb.append([InlineKeyboardButton(toggle_text, callback_data=f"mode_{sid}")])
    kb.append([InlineKeyboardButton("🗑 Delete", callback_data=f"del_{sid}")])
    if active:
        viewers_count = len(viewers.get(sid, set()))
        uptime = "00:00:00"
        if s.get("start_time"):
            uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
        mode_label = "COPY" if mode == "copy" else "TRANSCODE"
        info = f"FPS:{s.get('fps','?')} | 👥{viewers_count} | ⏱️{uptime} | {mode_label}"
        kb.append([InlineKeyboardButton(info, callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

async def update_panel(sid, bot):
    s = streams.get(sid)
    if not s or not s.get("chat_id") or not s.get("message_id"):
        return
    try:
        text = get_panel_text(sid)
        await bot.edit_message_text(
            chat_id=s["chat_id"],
            message_id=s["message_id"],
            text=text,
            reply_markup=stream_panel_keyboard(sid, s),
            parse_mode="Markdown"
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            print(f"Panel update error: {e}")

# =========================================
# STREAM LISTS
# =========================================
def streams_inline_keyboard(stream_type):
    kb = []
    for sid, s in streams.items():
        if s.get("type") == stream_type:
            status = "🟢" if s.get("active") else "🔴"
            kb.append([InlineKeyboardButton(f"{status} {s['name']}", callback_data=f"open_{sid}")])
    if not kb:
        kb.append([InlineKeyboardButton("❌ No streams", callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

def inline_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 HLS List", callback_data="list_hls")],
        [InlineKeyboardButton("📡 RTMP List", callback_data="list_rtmp")],
        [InlineKeyboardButton("➕ Add Stream", callback_data="add_stream")],
        [InlineKeyboardButton("🖥 Server Status", callback_data="monitor_server")],
        [InlineKeyboardButton("🧹 Clean Files", callback_data="clean_files")]
    ])

reply_kb = ReplyKeyboardMarkup([
    ["📺 HLS List", "📡 RTMP List"],
    ["➕ Add Stream", "🖥 Server Status"],
    ["🧹 Clean Files"]
], resize_keyboard=True)

# =========================================
# FFMPEG STREAM (Robust)
# =========================================
async def run_ffmpeg(sid, bot):
    s = streams[sid]
    src = s["source"]
    mode = s.get("mode", "copy")
    ua = s.get("ua", "ExoPlayerLib/2.18.5")
    logo = s.get("logo", "")
    typ = s.get("type", "hls")

    # Create stream directory with absolute path
    stream_dir = os.path.join(HLS_DIR, sid)
    if os.path.exists(stream_dir):
        shutil.rmtree(stream_dir, ignore_errors=True)
    os.makedirs(stream_dir, exist_ok=True)
    out_file = os.path.join(stream_dir, "index.m3u8")

    # Common options
    base = [
        "ffmpeg", "-loglevel", "warning",
        "-re",
        "-user_agent", ua,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-timeout", "10000000", "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "500000", "-probesize", "5000000",
        "-i", src
    ]

    if mode == "copy":
        video = ["-c:v", "copy"]
        filter_complex = None
    else:
        video = [
            "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
            "-b:v", VIDEO_BITRATE, "-threads", "2",
            "-tune", "fastdecode"
        ]
        # Scale only if necessary
        filter_complex = "[0:v]scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease"
        if logo and len(logo) > 5:
            filter_complex = f"[1:v]scale=120:-1[logo];[0:v]scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease[bg];[bg][logo]overlay=W-w-15:H-h-15"

    audio = ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2"]

    if typ == "hls":
        if filter_complex and logo and len(logo) > 5:
            cmd = base + ["-i", logo, "-filter_complex", filter_complex] + video + audio + [
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", "-y", out_file
            ]
        elif filter_complex:
            cmd = base + ["-filter_complex", filter_complex] + video + audio + [
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", "-y", out_file
            ]
        else:
            cmd = base + video + audio + [
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", "-y", out_file
            ]
    else:  # RTMP
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        if filter_complex and logo and len(logo) > 5:
            cmd = base + ["-i", logo, "-filter_complex", filter_complex] + video + audio + ["-f", "flv", "-y", rtmp_url]
        elif filter_complex:
            cmd = base + ["-filter_complex", filter_complex] + video + audio + ["-f", "flv", "-y", rtmp_url]
        else:
            cmd = base + video + audio + ["-f", "flv", "-y", rtmp_url]

    # Kill any previous process
    if sid in processes:
        try:
            processes[sid].terminate()
            await asyncio.sleep(1)
            processes[sid].kill()
        except: pass
        processes.pop(sid, None)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )
    processes[sid] = proc
    s["active"] = True
    s["start_time"] = time.time()
    save_streams()
    await update_panel(sid, bot)

    # Read stderr for fps info
    async def read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            txt = line.decode(errors="ignore").strip()
            m = re.search(r"fps=\s*([\d.]+)", txt)
            if m:
                s["fps"] = m.group(1)
                await update_panel(sid, bot)
            if "error" in txt.lower():
                print(f"[{sid}] ffmpeg: {txt}")
    asyncio.create_task(read_stderr())

    # Wait for process to finish
    await proc.wait()
    s["active"] = False
    processes.pop(sid, None)
    save_streams()
    await update_panel(sid, bot)

async def stop_stream(sid, bot):
    if sid in processes:
        try:
            processes[sid].terminate()
            await asyncio.sleep(1)
            processes[sid].kill()
        except: pass
        processes.pop(sid, None)
    if sid in streams:
        streams[sid]["active"] = False
        save_streams()
    # Clean HLS directory
    stream_dir = os.path.join(HLS_DIR, sid)
    if os.path.exists(stream_dir):
        shutil.rmtree(stream_dir, ignore_errors=True)
    await update_panel(sid, bot)

# =========================================
# MONITOR LOOP (updates every 20 sec)
# =========================================
async def monitor_loop(update, context, chat_id, message_id):
    global monitor_active
    while monitor_active:
        status = system_status()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ Stop Monitor", callback_data="stop_monitor")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
        ])
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=status,
                reply_markup=kb,
                parse_mode="Markdown"
            )
        except: pass
        await asyncio.sleep(20)

# =========================================
# SAFE CALLBACK HELPERS
# =========================================
async def safe_answer(query, text=None, alert=False):
    try:
        if text:
            await query.answer(text, show_alert=alert)
        else:
            await query.answer()
    except BadRequest as e:
        if "Query is too old" in str(e):
            pass
        else:
            print(f"Answer error: {e}")
    except: pass

async def safe_edit(query, text, reply_markup=None, parse=None):
    try:
        if reply_markup:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse)
        else:
            await query.edit_message_text(text, parse_mode=parse)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            print(f"Edit error: {e}")
    except: pass

# =========================================
# CALLBACK HANDLER
# =========================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_active, monitor_task
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    chat_id = query.message.chat_id
    msg_id = query.message.message_id

    if data == "main_menu":
        if monitor_active:
            monitor_active = False
            if monitor_task:
                monitor_task.cancel()
        await safe_edit(query, "🎬 Main Menu", reply_markup=inline_main_menu())
        return

    if data == "stop_monitor":
        if monitor_active:
            monitor_active = False
            if monitor_task:
                monitor_task.cancel()
        await safe_edit(query, system_status(), reply_markup=inline_main_menu(), parse="Markdown")
        return

    if data == "list_hls":
        await safe_edit(query, "📺 HLS Streams:", reply_markup=streams_inline_keyboard("hls"))
        return
    if data == "list_rtmp":
        await safe_edit(query, "📡 RTMP Streams:", reply_markup=streams_inline_keyboard("rtmp"))
        return

    if data == "add_stream":
        context.user_data["step"] = "add_name"
        await safe_edit(query, "📝 Send stream name:")
        return

    if data == "monitor_server":
        if monitor_active:
            monitor_active = False
            if monitor_task:
                monitor_task.cancel()
            await safe_edit(query, system_status(), reply_markup=inline_main_menu(), parse="Markdown")
            return
        status = system_status()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ Stop Monitor", callback_data="stop_monitor")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
        ])
        await safe_edit(query, status, reply_markup=kb, parse="Markdown")
        monitor_active = True
        monitor_task = asyncio.create_task(monitor_loop(update, context, chat_id, msg_id))
        return

    if data == "clean_files":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        await safe_edit(query, "✅ HLS directory cleaned", reply_markup=inline_main_menu())
        return

    if data.startswith("open_"):
        sid = data[5:]
        if sid in streams:
            streams[sid]["chat_id"] = chat_id
            streams[sid]["message_id"] = msg_id
            save_streams()
            text = get_panel_text(sid)
            await safe_edit(query, text, reply_markup=stream_panel_keyboard(sid, streams[sid]), parse="Markdown")
        else:
            await safe_edit(query, "❌ Stream not found")
        return

    if data.startswith("start_"):
        sid = data[6:]
        s = streams.get(sid)
        if not s:
            await safe_answer(query, "❌ Not found", alert=True)
            return
        if not s.get("source"):
            await safe_answer(query, "❌ No source!", alert=True)
            return
        if s["type"] == "rtmp" and (not s.get("rtmp_server") or not s.get("rtmp_key")):
            await safe_answer(query, "❌ RTMP settings incomplete", alert=True)
            return
        if s.get("active"):
            await safe_answer(query, "⚠️ Already running", alert=True)
            return
        await safe_answer(query, "⏳ Starting...")
        asyncio.create_task(run_ffmpeg(sid, context.bot))
        return

    if data.startswith("stop_"):
        sid = data[5:]
        await safe_answer(query, "⏹ Stopping...")
        asyncio.create_task(stop_stream(sid, context.bot))
        return

    if data.startswith("source_"):
        sid = data[7:]
        context.user_data["edit"] = ("source", sid, chat_id, msg_id)
        await safe_edit(query, "📥 Send new source URL:")
        return

    if data.startswith("logo_"):
        sid = data[5:]
        context.user_data["edit"] = ("logo", sid, chat_id, msg_id)
        await safe_edit(query, "🖼 Send logo image URL (or /skip):")
        return

    if data.startswith("ua_"):
        sid = data[3:]
        context.user_data["edit"] = ("ua", sid, chat_id, msg_id)
        await safe_edit(query, "🕵️ Send User-Agent (or /skip):")
        return

    if data.startswith("rename_"):
        sid = data[7:]
        context.user_data["edit"] = ("rename", sid, chat_id, msg_id)
        await safe_edit(query, "✏️ Send new name:")
        return

    if data.startswith("rtmpsrv_"):
        sid = data[8:]
        context.user_data["edit"] = ("rtmp_server", sid, chat_id, msg_id)
        await safe_edit(query, "📡 Send RTMP server URL:")
        return

    if data.startswith("rtmpkey_"):
        sid = data[8:]
        context.user_data["edit"] = ("rtmp_key", sid, chat_id, msg_id)
        await safe_edit(query, "🔑 Send stream key:")
        return

    if data.startswith("mode_"):
        sid = data[5:]
        if sid in streams:
            old = streams[sid]["mode"]
            new = "encode" if old == "copy" else "copy"
            streams[sid]["mode"] = new
            save_streams()
            await safe_answer(query, f"✅ Switched to {new}")
            if streams[sid].get("active"):
                await stop_stream(sid, context.bot)
                asyncio.create_task(run_ffmpeg(sid, context.bot))
            else:
                await update_panel(sid, context.bot)
        else:
            await safe_answer(query, "❌ Error", alert=True)
        return

    if data.startswith("del_"):
        sid = data[4:]
        await stop_stream(sid, context.bot)
        if sid in streams:
            del streams[sid]
            save_streams()
        await safe_edit(query, "🗑 Stream deleted", reply_markup=inline_main_menu())
        return

# =========================================
# TEXT MESSAGE HANDLER
# =========================================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized")
        return

    text = update.message.text

    if text == "📺 HLS List":
        await update.message.reply_text("📺 HLS Streams:", reply_markup=streams_inline_keyboard("hls"))
        return
    if text == "📡 RTMP List":
        await update.message.reply_text("📡 RTMP Streams:", reply_markup=streams_inline_keyboard("rtmp"))
        return
    if text == "➕ Add Stream":
        context.user_data["step"] = "add_name"
        await update.message.reply_text("📝 Send stream name:")
        return
    if text == "🖥 Server Status":
        status = system_status()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ Stop Monitor", callback_data="stop_monitor")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
        ])
        msg = await update.message.reply_text(status, reply_markup=kb, parse_mode="Markdown")
        monitor_active = True
        monitor_task = asyncio.create_task(monitor_loop(update, context, msg.chat_id, msg.message_id))
        return
    if text == "🧹 Clean Files":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        await update.message.reply_text("✅ HLS directory cleaned")
        return

    if context.user_data.get("step") == "add_name":
        name = text.strip()
        base_sid = name.replace(" ", "_")
        sid = base_sid
        counter = 1
        while sid in streams:
            sid = f"{base_sid}_{counter}"
            counter += 1
        streams[sid] = {
            "name": name,
            "source": "",
            "type": "hls",
            "mode": "copy",
            "active": False,
            "fps": "?",
            "ua": "ExoPlayerLib/2.18.5",
            "logo": "",
            "rtmp_server": "",
            "rtmp_key": "",
            "chat_id": None,
            "message_id": None,
            "start_time": 0
        }
        save_streams()
        context.user_data["step"] = "add_source"
        context.user_data["sid"] = sid
        await update.message.reply_text("📥 Send source URL:")
        return

    if context.user_data.get("step") == "add_source":
        sid = context.user_data.get("sid")
        if sid in streams:
            streams[sid]["source"] = text
            save_streams()
            context.user_data.pop("step")
            context.user_data.pop("sid")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("HLS", callback_data=f"settype_{sid}_hls")],
                [InlineKeyboardButton("RTMP", callback_data=f"settype_{sid}_rtmp")]
            ])
            await update.message.reply_text("Choose stream type:", reply_markup=kb)
        else:
            await update.message.reply_text("❌ Error")
        return

    if context.user_data.get("edit"):
        typ, sid, edit_chat_id, edit_msg_id = context.user_data["edit"]
        s = streams.get(sid)
        if not s:
            await update.message.reply_text("❌ Stream not found")
            context.user_data.pop("edit")
            return
        if typ == "source":
            s["source"] = text
        elif typ == "logo":
            s["logo"] = "" if text == "/skip" else text
        elif typ == "ua":
            s["ua"] = "ExoPlayerLib/2.18.5" if text == "/skip" else text
        elif typ == "rename":
            old = s["name"]
            s["name"] = text
            await update.message.reply_text(f"✅ Renamed {old} → {text}")
        elif typ == "rtmp_server":
            s["rtmp_server"] = text
        elif typ == "rtmp_key":
            s["rtmp_key"] = text
        save_streams()
        context.user_data.pop("edit")
        await update_panel(sid, context.bot)
        await update.message.delete()
        return

# =========================================
# SET TYPE CALLBACK
# =========================================
async def set_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.split("_", 2)
    if len(parts) != 3:
        await safe_edit(query, "❌ Invalid")
        return
    _, sid, typ = parts
    if sid in streams:
        streams[sid]["type"] = typ
        if typ == "rtmp":
            context.user_data["edit"] = ("rtmp_server", sid, query.message.chat_id, query.message.message_id)
            await safe_edit(query, "📡 Send RTMP server URL:")
        else:
            save_streams()
            text = get_panel_text(sid)
            await safe_edit(query, text, reply_markup=stream_panel_keyboard(sid, streams[sid]), parse="Markdown")
    else:
        await safe_edit(query, "❌ Error")

# =========================================
# START COMMAND
# =========================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized")
        return
    await update.message.reply_text(
        "🎬 **Live Stream Bot**\n\n"
        "Use the buttons below:\n"
        "📺 HLS List - Show HLS streams\n"
        "📡 RTMP List - Show RTMP streams\n"
        "➕ Add Stream - Create new stream\n"
        "🖥 Server Status - Monitor CPU/RAM/Disk\n"
        "🧹 Clean Files - Clear HLS cache\n\n"
        "To open a stream panel, click on its name in the lists.",
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
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(callback_handler, pattern="^(?!settype_).*"))
    app.add_handler(CallbackQueryHandler(set_type, pattern="^settype_"))

    print("🚀 Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()