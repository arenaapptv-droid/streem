import asyncio
import json
import os
import re
import shutil
import time
from collections import defaultdict

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import BadRequest

# =========================================
# CONFIG - كل الإعدادات هنا
# =========================================
with open("settings.json") as f:
    cfg = json.load(f)

TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]
BASE_URL = cfg.get("BASE_URL", "http://164.68.102.28")
PORT = cfg.get("PORT", 8080)

# إعدادات الترميز (ثابتة في ملف settings.json)
VIDEO_BITRATE = cfg.get("VIDEO_BITRATE", "4000k")
AUDIO_BITRATE = cfg.get("AUDIO_BITRATE", "128k")
PRESET = cfg.get("PRESET", "ultrafast")
CRF = cfg.get("CRF", 28)
THREADS = cfg.get("THREADS", 1)
TUNE = cfg.get("TUNE", "fastdecode")

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

def save():
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
# HTTP SERVER
# =========================================
async def hls_handler(request):
    sid = request.match_info["name"]
    filename = request.match_info.get("file", "index.m3u8")
    path = os.path.join(HLS_DIR, sid, filename)
    if not os.path.exists(path):
        return web.Response(status=404)
    ip = request.headers.get("X-Forwarded-For", request.remote)
    if ip:
        viewers[sid].add(ip)
        viewer_last[sid][ip] = time.time()
    return web.FileResponse(path)

async def start_http():
    app = web.Application()
    app.router.add_get("/live/{name}/{file:.*}", hls_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"✅ HTTP on {PORT}")

# =========================================
# SYSTEM STATUS
# =========================================
def system_status():
    try:
        import psutil
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        return f"🖥 CPU: {cpu}%\n🧠 RAM: {mem.percent}%\n📺 Streams: {len(streams)}"
    except:
        return f"📺 Streams: {len(streams)}"

# =========================================
# UI
# =========================================
reply_kb = ReplyKeyboardMarkup([
    ["📺 HLS List", "📡 RTMP List"],
    ["➕ Add Stream", "🖥 Server Status"],
    ["🧹 Clean Files"]
], resize_keyboard=True)

def inline_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 HLS List", callback_data="list_hls")],
        [InlineKeyboardButton("📡 RTMP List", callback_data="list_rtmp")],
        [InlineKeyboardButton("➕ Add Stream", callback_data="add_stream")],
        [InlineKeyboardButton("🖥 Server Status", callback_data="monitor")],
        [InlineKeyboardButton("🧹 Clean Files", callback_data="clean")]
    ])

def streams_list(typ):
    kb = []
    for sid, s in streams.items():
        if s["type"] == typ:
            status = "🟢" if s.get("active") else "🔴"
            kb.append([InlineKeyboardButton(f"{status} {s['name']}", callback_data=f"open_{sid}")])
    kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

def panel_keyboard(sid, s):
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
    toggle = "🔄 Copy" if mode == "encode" else "⚙️ Transcode"
    kb.append([InlineKeyboardButton(toggle, callback_data=f"mode_{sid}")])
    kb.append([InlineKeyboardButton("🗑 Delete", callback_data=f"del_{sid}")])
    if active:
        viewers_count = len(viewers.get(sid, set()))
        uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
        kb.append([InlineKeyboardButton(f"FPS:{s.get('fps','?')} | 👥{viewers_count} | ⏱️{uptime}", callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

async def update_panel(sid, bot):
    s = streams.get(sid)
    if not s or not s.get("chat_id") or not s.get("message_id"):
        return
    text = (
        f"🎛️ **{s['name']}**\n"
        f"📥 Source: `{s['source']}`\n"
        f"🖼 Logo: {'✅' if s.get('logo') else '❌'}\n"
        f"⚙️ Mode: {'Copy' if s['mode']=='copy' else 'Transcode'}\n"
        f"🟢 Status: {'Running' if s.get('active') else 'Stopped'}\n"
        f"🎬 FPS: {s.get('fps','?')}\n"
        f"👥 Viewers: {len(viewers.get(sid, set()))}\n"
        f"⏱️ Uptime: {time.strftime('%H:%M:%S', time.gmtime(time.time() - s['start_time'])) if s.get('start_time') else '00:00:00'}\n"
    )
    if s["type"] == "hls":
        text += f"\n🔗 {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    else:
        text += f"\n📡 {s.get('rtmp_server')}/{s.get('rtmp_key')}"
    try:
        await bot.edit_message_text(text, chat_id=s["chat_id"], message_id=s["message_id"],
                                    reply_markup=panel_keyboard(sid, s), parse_mode="Markdown")
    except: pass

# =========================================
# FFMPEG STREAM - باستخدام الإعدادات من settings.json
# =========================================
async def start_stream(sid, bot):
    s = streams[sid]
    src = s["source"]
    mode = s["mode"]
    ua = s.get("ua", "ExoPlayerLib/2.18.5")
    logo = s.get("logo", "")
    typ = s["type"]

    # clean dir
    stream_dir = os.path.join(HLS_DIR, sid)
    if os.path.exists(stream_dir):
        shutil.rmtree(stream_dir, ignore_errors=True)
    os.makedirs(stream_dir, exist_ok=True)
    out_file = os.path.join(stream_dir, "index.m3u8")

    base = [
        "ffmpeg", "-loglevel", "warning", "-re",
        "-user_agent", ua,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-timeout", "10000000", "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "500000", "-probesize", "5000000",
        "-i", src
    ]

    if mode == "copy":
        video = ["-c:v", "copy"]
        filter_cmd = []
    else:
        # استخدام الإعدادات من settings.json
        video = ["-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
                 "-b:v", VIDEO_BITRATE, "-threads", str(THREADS), "-tune", TUNE]
        if logo and len(logo) > 5:
            filter_cmd = ["-i", logo, "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo][ref];[ref][logo]overlay=0:0"]
        else:
            filter_cmd = ["-filter_complex", "[0:v]scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease"]

    audio = ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2"]

    if typ == "hls":
        cmd = base + filter_cmd + video + audio + [
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments", "-y", out_file
        ]
    else:
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        cmd = base + filter_cmd + video + audio + ["-f", "flv", "-y", rtmp_url]

    if sid in processes:
        try:
            processes[sid].terminate()
            await asyncio.sleep(1)
            processes[sid].kill()
        except: pass

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    processes[sid] = proc
    s["active"] = True
    s["start_time"] = time.time()
    save()
    await update_panel(sid, bot)

    async def read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line: break
            txt = line.decode(errors="ignore")
            m = re.search(r"fps=\s*([\d.]+)", txt)
            if m:
                s["fps"] = m.group(1)
                await update_panel(sid, bot)
    asyncio.create_task(read_stderr())

    await proc.wait()
    s["active"] = False
    processes.pop(sid, None)
    save()
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
        save()
    stream_dir = os.path.join(HLS_DIR, sid)
    if os.path.exists(stream_dir):
        shutil.rmtree(stream_dir, ignore_errors=True)
    await update_panel(sid, bot)

# =========================================
# CALLBACK HANDLER
# =========================================
async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    chat_id = q.message.chat_id
    msg_id = q.message.message_id

    try:
        await q.answer()
    except: pass

    if data == "main_menu":
        await q.edit_message_text("🎬 Main Menu", reply_markup=inline_main())
        return

    if data == "list_hls":
        await q.edit_message_text("📺 HLS Streams:", reply_markup=streams_list("hls"))
        return
    if data == "list_rtmp":
        await q.edit_message_text("📡 RTMP Streams:", reply_markup=streams_list("rtmp"))
        return
    if data == "add_stream":
        context.user_data["step"] = "add_name"
        await q.edit_message_text("📝 Send stream name:")
        return
    if data == "monitor":
        await q.edit_message_text(system_status(), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]))
        return
    if data == "clean":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        await q.edit_message_text("✅ Cleaned", reply_markup=inline_main())
        return

    if data.startswith("open_"):
        sid = data[5:]
        if sid in streams:
            streams[sid]["chat_id"] = chat_id
            streams[sid]["message_id"] = msg_id
            save()
            await update_panel(sid, context.bot)
        else:
            await q.edit_message_text("❌ Not found")
        return

    if data.startswith("start_"):
        sid = data[6:]
        if sid in streams:
            s = streams[sid]
            if not s.get("source"):
                await q.answer("❌ No source!", show_alert=True)
                return
            if s["type"] == "rtmp" and (not s.get("rtmp_server") or not s.get("rtmp_key")):
                await q.answer("❌ RTMP incomplete!", show_alert=True)
                return
            if s.get("active"):
                await q.answer("⚠️ Already running", show_alert=True)
                return
            await q.answer("⏳ Starting...")
            asyncio.create_task(start_stream(sid, context.bot))
        return

    if data.startswith("stop_"):
        sid = data[5:]
        await q.answer("⏹ Stopping...")
        asyncio.create_task(stop_stream(sid, context.bot))
        return

    if data.startswith("source_"):
        sid = data[7:]
        context.user_data["edit"] = ("source", sid, chat_id, msg_id)
        await q.edit_message_text("📥 Send new source URL:")
        return
    if data.startswith("logo_"):
        sid = data[5:]
        context.user_data["edit"] = ("logo", sid, chat_id, msg_id)
        await q.edit_message_text("🖼 Send logo URL (or /skip):")
        return
    if data.startswith("ua_"):
        sid = data[3:]
        context.user_data["edit"] = ("ua", sid, chat_id, msg_id)
        await q.edit_message_text("🕵️ Send User-Agent (or /skip):")
        return
    if data.startswith("rename_"):
        sid = data[7:]
        context.user_data["edit"] = ("rename", sid, chat_id, msg_id)
        await q.edit_message_text("✏️ Send new name:")
        return
    if data.startswith("rtmpsrv_"):
        sid = data[8:]
        context.user_data["edit"] = ("rtmp_server", sid, chat_id, msg_id)
        await q.edit_message_text("📡 Send RTMP server URL:")
        return
    if data.startswith("rtmpkey_"):
        sid = data[8:]
        context.user_data["edit"] = ("rtmp_key", sid, chat_id, msg_id)
        await q.edit_message_text("🔑 Send stream key:")
        return

    if data.startswith("mode_"):
        sid = data[5:]
        if sid in streams:
            old = streams[sid]["mode"]
            new = "encode" if old == "copy" else "copy"
            streams[sid]["mode"] = new
            save()
            await q.answer(f"✅ Switched to {new}")
            if streams[sid].get("active"):
                await stop_stream(sid, context.bot)
                asyncio.create_task(start_stream(sid, context.bot))
            else:
                await update_panel(sid, context.bot)
        return

    if data.startswith("del_"):
        sid = data[4:]
        await stop_stream(sid, context.bot)
        if sid in streams:
            del streams[sid]
            save()
        await q.edit_message_text("🗑 Deleted", reply_markup=inline_main())
        return

# =========================================
# TEXT HANDLER
# =========================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = update.message.text

    # Adding new stream
    if context.user_data.get("step") == "add_name":
        name = text.strip()
        sid = name.replace(" ", "_")
        if sid in streams:
            counter = 1
            while f"{sid}_{counter}" in streams:
                counter += 1
            sid = f"{sid}_{counter}"
        streams[sid] = {
            "name": name, "source": "", "type": "hls", "mode": "copy", "active": False,
            "fps": "?", "ua": "ExoPlayerLib/2.18.5", "logo": "", "rtmp_server": "",
            "rtmp_key": "", "chat_id": None, "message_id": None, "start_time": 0
        }
        save()
        context.user_data["step"] = "add_source"
        context.user_data["sid"] = sid
        await update.message.reply_text("📥 Send source URL:")
        return

    if context.user_data.get("step") == "add_source":
        sid = context.user_data.get("sid")
        if sid in streams:
            streams[sid]["source"] = text
            save()
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

    # Editing stream parameter
    if context.user_data.get("edit"):
        typ, sid, edit_chat, edit_msg = context.user_data["edit"]
        s = streams.get(sid)
        if not s:
            context.user_data.pop("edit")
            return
        if typ == "source":
            s["source"] = text
        elif typ == "logo":
            s["logo"] = "" if text == "/skip" else text
        elif typ == "ua":
            s["ua"] = "ExoPlayerLib/2.18.5" if text == "/skip" else text
        elif typ == "rename":
            s["name"] = text
        elif typ == "rtmp_server":
            s["rtmp_server"] = text
        elif typ == "rtmp_key":
            s["rtmp_key"] = text
        save()
        context.user_data.pop("edit")
        await update_panel(sid, context.bot)
        await update.message.delete()
        return

# =========================================
# SET TYPE
# =========================================
async def set_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except: pass
    _, sid, typ = q.data.split("_", 2)
    if sid in streams:
        streams[sid]["type"] = typ
        if typ == "rtmp":
            context.user_data["edit"] = ("rtmp_server", sid, q.message.chat_id, q.message.message_id)
            await q.edit_message_text("📡 Send RTMP server URL:")
        else:
            save()
            await update_panel(sid, context.bot)
    else:
        await q.edit_message_text("❌ Error")

# =========================================
# START
# =========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized")
        return
    await update.message.reply_text("🎬 **Live Stream Bot**\nUse the buttons below.", reply_markup=reply_kb)

# =========================================
# MAIN
# =========================================
async def main():
    await start_http()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(callback, pattern="^(?!settype_).*"))
    app.add_handler(CallbackQueryHandler(set_type, pattern="^settype_"))
    print("🚀 Bot running...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())