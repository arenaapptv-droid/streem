import asyncio
import json
import os
import re
import shutil
import time
from collections import defaultdict

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# =========================================
# PSUTIL for system monitor
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
# KEYBOARD - MAIN (ReplyKeyboard)
# =========================================
main_kb = ReplyKeyboardMarkup([
    ["📺 قائمة HLS", "📡 قائمة RTMP"],
    ["➕ إضافة بث", "🖥 مراقبة السيرفر"],
    ["🧹 تنظيف الملفات"]
], resize_keyboard=True)

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
    print(f"✅ HTTP server on port {PORT}")

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
# SYSTEM STATUS
# =========================================
def system_status():
    if PSUTIL_AVAILABLE:
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return (
            f"🖥️ **حالة السيرفر**\n"
            f"💻 CPU: {cpu}%\n"
            f"🧠 RAM: {mem.percent}% ({mem.used//(1024**2)}/{mem.total//(1024**2)} MB)\n"
            f"💾 Disk: {disk.percent}% ({disk.used//(1024**3)}/{disk.total//(1024**3)} GB)\n"
            f"📺 البثوث: {len(streams)}"
        )
    else:
        load = "N/A"
        try:
            with open("/proc/loadavg") as f:
                load = f.read().split()[0]
        except:
            pass
        return f"🖥️ الحمل: {load}\n📺 البثوث: {len(streams)}"

# =========================================
# INLINE KEYBOARD FOR STREAMS LISTS
# =========================================
def streams_inline_keyboard(stream_type):
    kb = []
    for sid, s in streams.items():
        if s.get("type") == stream_type:
            status = "🟢" if s.get("active") else "🔴"
            kb.append([InlineKeyboardButton(f"{status} {s['name']}", callback_data=f"panel_{sid}")])
    if not kb:
        kb.append([InlineKeyboardButton("❌ لا توجد بثوث", callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

# =========================================
# STREAM PANEL (INLINE KEYS FOR CONTROLS)
# =========================================
def stream_panel_inline_keyboard(sid, s):
    active = s.get("active", False)
    mode = s.get("mode", "copy")
    typ = s.get("type", "hls")
    kb = []
    if active:
        kb.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{sid}")])
    else:
        kb.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{sid}")])
    kb.append([
        InlineKeyboardButton("📥 مصدر", callback_data=f"source_{sid}"),
        InlineKeyboardButton("🖼 شعار", callback_data=f"logo_{sid}")
    ])
    kb.append([
        InlineKeyboardButton("🕵️ UA", callback_data=f"ua_{sid}"),
        InlineKeyboardButton("✏️ اسم", callback_data=f"rename_{sid}")
    ])
    if typ == "rtmp":
        kb.append([
            InlineKeyboardButton("📡 RTMP سيرفر", callback_data=f"rtmpsrv_{sid}"),
            InlineKeyboardButton("🔑 RTMP مفتاح", callback_data=f"rtmpkey_{sid}")
        ])
    toggle_text = "🔄 نسخ مباشر" if mode == "encode" else "⚙️ ترميز (جودة عالية)"
    kb.append([InlineKeyboardButton(toggle_text, callback_data=f"mode_{sid}")])
    kb.append([InlineKeyboardButton("🗑 حذف", callback_data=f"del_{sid}")])
    if active:
        viewers_count = len(viewers.get(sid, set()))
        uptime = "00:00:00"
        if s.get("start_time"):
            uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
        mode_label = "نسخ" if mode == "copy" else "ترميز"
        info = f"FPS:{s.get('fps','?')} | 👥{viewers_count} | ⏱️{uptime} | {mode_label}"
        kb.append([InlineKeyboardButton(info, callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 القوائم", callback_data="back_to_lists")])
    return InlineKeyboardMarkup(kb)

async def send_panel(chat_id, sid, bot):
    s = streams.get(sid)
    if not s:
        await bot.send_message(chat_id, "❌ البث غير موجود")
        return
    uptime = "00:00:00"
    if s.get("start_time"):
        uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
    viewers_count = len(viewers.get(sid, set()))
    text = (
        f"🎛️ **{s['name']}**\n"
        f"📥 المصدر: `{s['source']}`\n"
        f"🖼 الشعار: {'✅' if s.get('logo') else '❌'}\n"
        f"🕵️ UA: `{s.get('ua')}`\n"
        f"⚙️ الوضع: {'نسخ مباشر' if s['mode']=='copy' else 'ترميز (جودة عالية)'}\n"
        f"🟢 الحالة: {'يعمل' if s.get('active') else 'متوقف'}\n"
        f"🎬 FPS: {s.get('fps','?')}\n"
        f"👥 المشاهدين: {viewers_count}\n"
        f"⏱️ التشغيل: {uptime}\n"
    )
    if s["type"] == "hls":
        text += f"\n🔗 HLS: {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    else:
        text += f"\n📡 RTMP: {s.get('rtmp_server')}/{s.get('rtmp_key')}"
    await bot.send_message(chat_id, text, reply_markup=stream_panel_inline_keyboard(sid, s), parse_mode="Markdown")

# =========================================
# FFMPEG STREAM (fixed encoding)
# =========================================
async def start_stream(sid, chat_id, bot):
    s = streams[sid]
    src = s["source"]
    mode = s.get("mode", "copy")
    ua = s.get("ua", "ExoPlayerLib/2.18.5")
    logo = s.get("logo", "")
    typ = s.get("type", "hls")

    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "index.m3u8")

    # Common options for stability and preload
    base_cmd = [
        "ffmpeg", "-re",
        "-user_agent", ua,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
        "-timeout", "10000000", "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "1000000", "-probesize", "5000000",
        "-stream_loop", "-1",
        "-i", src
    ]

    if mode == "copy":
        video_opts = ["-c:v", "copy"]
    else:
        # Encode with high quality (max 1080p, 9Mbps, keyframe every 3s)
        video_opts = [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
            "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
            "-g", "90"
        ]

    audio_opts = ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]

    if typ == "hls":
        if logo and mode != "copy":
            cmd = base_cmd + ["-i", logo, "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0"] + video_opts + audio_opts + [
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", out_file
            ]
        else:
            cmd = base_cmd + video_opts + audio_opts + [
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", out_file
            ]
    else:  # RTMP
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        if logo and mode != "copy":
            cmd = base_cmd + ["-i", logo, "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0"] + video_opts + audio_opts + [
                "-f", "flv", rtmp_url
            ]
        else:
            cmd = base_cmd + video_opts + audio_opts + ["-f", "flv", rtmp_url]

    # kill old process
    if sid in processes:
        try:
            processes[sid].terminate()
            await asyncio.sleep(0.5)
            processes[sid].kill()
        except:
            pass

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )
    processes[sid] = proc
    s["active"] = True
    s["start_time"] = time.time()
    save_streams()

    async def stderr_reader():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            txt = line.decode(errors="ignore").strip()
            m = re.search(r"fps=\s*([\d.]+)", txt)
            if m:
                s["fps"] = m.group(1)
            if "error" in txt.lower():
                print(f"[{sid}] ffmpeg: {txt}")
    asyncio.create_task(stderr_reader())

    await proc.wait()
    s["active"] = False
    processes.pop(sid, None)
    save_streams()
    await bot.send_message(chat_id, f"⛔ توقف بث {s['name']}")

async def stop_stream(sid):
    if sid in processes:
        try:
            processes[sid].terminate()
            await asyncio.sleep(0.5)
            processes[sid].kill()
        except:
            pass
        processes.pop(sid, None)
    if sid in streams:
        streams[sid]["active"] = False
        save_streams()
    path = os.path.join(HLS_DIR, sid)
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)

# =========================================
# CALLBACK QUERY HANDLER (for inline buttons)
# =========================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "main_menu":
        await query.edit_message_text("🎬 القائمة الرئيسية", reply_markup=main_kb)
        return

    if data == "back_to_lists":
        # Show HLS/RTMP selection menu (inline)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📺 قائمة HLS", callback_data="list_hls")],
            [InlineKeyboardButton("📡 قائمة RTMP", callback_data="list_rtmp")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
        ])
        await query.edit_message_text("اختر نوع البث:", reply_markup=kb)
        return

    if data == "list_hls":
        await query.edit_message_text("📺 بثوث HLS:", reply_markup=streams_inline_keyboard("hls"))
        return
    if data == "list_rtmp":
        await query.edit_message_text("📡 بثوث RTMP:", reply_markup=streams_inline_keyboard("rtmp"))
        return

    if data.startswith("panel_"):
        sid = data[6:]
        if sid in streams:
            s = streams[sid]
            uptime = "00:00:00"
            if s.get("start_time"):
                uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
            viewers_count = len(viewers.get(sid, set()))
            text = (
                f"🎛️ **{s['name']}**\n"
                f"📥 المصدر: `{s['source']}`\n"
                f"🖼 الشعار: {'✅' if s.get('logo') else '❌'}\n"
                f"🕵️ UA: `{s.get('ua')}`\n"
                f"⚙️ الوضع: {'نسخ مباشر' if s['mode']=='copy' else 'ترميز'}\n"
                f"🟢 الحالة: {'يعمل' if s.get('active') else 'متوقف'}\n"
                f"🎬 FPS: {s.get('fps','?')}\n"
                f"👥 المشاهدين: {viewers_count}\n"
                f"⏱️ التشغيل: {uptime}\n"
            )
            if s["type"] == "hls":
                text += f"\n🔗 HLS: {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
            else:
                text += f"\n📡 RTMP: {s.get('rtmp_server')}/{s.get('rtmp_key')}"
            await query.edit_message_text(text, reply_markup=stream_panel_inline_keyboard(sid, s), parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ البث غير موجود")

    elif data.startswith("start_"):
        sid = data[6:]
        if sid in streams:
            s = streams[sid]
            if not s.get("source"):
                await query.answer("❌ لا يوجد مصدر!", show_alert=True)
                return
            if s["type"] == "rtmp" and (not s.get("rtmp_server") or not s.get("rtmp_key")):
                await query.answer("❌ إعدادات RTMP غير مكتملة!", show_alert=True)
                return
            if s.get("active"):
                await query.answer("⚠️ البث يعمل بالفعل", show_alert=True)
                return
            asyncio.create_task(start_stream(sid, chat_id, context.bot))
            await query.answer("⏳ جاري التشغيل...")
        else:
            await query.answer("❌ خطأ")

    elif data.startswith("stop_"):
        sid = data[5:]
        await stop_stream(sid)
        await query.answer("⏹ تم الإيقاف")
        # Refresh panel
        if sid in streams:
            await send_panel(chat_id, sid, context.bot)

    elif data.startswith("source_"):
        sid = data[7:]
        context.user_data["edit"] = ("source", sid, chat_id)
        await query.edit_message_text("📥 أرسل رابط المصدر الجديد:")

    elif data.startswith("logo_"):
        sid = data[5:]
        context.user_data["edit"] = ("logo", sid, chat_id)
        await query.edit_message_text("🖼 أرسل رابط الشعار (أو /skip):")

    elif data.startswith("ua_"):
        sid = data[3:]
        context.user_data["edit"] = ("ua", sid, chat_id)
        await query.edit_message_text("🕵️ أرسل User-Agent (أو /skip):")

    elif data.startswith("rename_"):
        sid = data[7:]
        context.user_data["edit"] = ("rename", sid, chat_id)
        await query.edit_message_text("✏️ أرسل الاسم الجديد:")

    elif data.startswith("rtmpsrv_"):
        sid = data[8:]
        context.user_data["edit"] = ("rtmp_server", sid, chat_id)
        await query.edit_message_text("📡 أرسل خادم RTMP (مثال: rtmp://live.twitch.tv/app):")

    elif data.startswith("rtmpkey_"):
        sid = data[8:]
        context.user_data["edit"] = ("rtmp_key", sid, chat_id)
        await query.edit_message_text("🔑 أرسل مفتاح البث (stream key):")

    elif data.startswith("mode_"):
        sid = data[5:]
        if sid in streams:
            old = streams[sid]["mode"]
            streams[sid]["mode"] = "encode" if old == "copy" else "copy"
            save_streams()
            await query.answer(f"✅ تم التبديل إلى {'ترميز' if streams[sid]['mode']=='encode' else 'نسخ'}")
            # Refresh panel
            await send_panel(chat_id, sid, context.bot)
        else:
            await query.answer("❌ خطأ")

    elif data.startswith("del_"):
        sid = data[4:]
        await stop_stream(sid)
        if sid in streams:
            streams.pop(sid, None)
            save_streams()
        await query.edit_message_text("🗑 تم حذف البث", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")]]))

    else:
        await query.answer("لا شيء")

# =========================================
# MESSAGE HANDLER (for text inputs)
# =========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return

    text = update.message.text
    chat_id = update.effective_chat.id

    # Main menu buttons
    if text == "📺 قائمة HLS":
        await update.message.reply_text("📺 بثوث HLS:", reply_markup=streams_inline_keyboard("hls"))
        return
    elif text == "📡 قائمة RTMP":
        await update.message.reply_text("📡 بثوث RTMP:", reply_markup=streams_inline_keyboard("rtmp"))
        return
    elif text == "➕ إضافة بث":
        context.user_data["step"] = "add_name"
        await update.message.reply_text("📝 أرسل اسم البث الجديد:")
        return
    elif text == "🖥 مراقبة السيرفر":
        await update.message.reply_text(system_status())
        return
    elif text == "🧹 تنظيف الملفات":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        await update.message.reply_text("✅ تم تنظيف مجلد HLS")
        return

    # Steps for adding stream
    if context.user_data.get("step") == "add_name":
        name = text.strip()
        sid = name.replace(" ", "_") + str(int(time.time()))
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
            "rtmp_key": ""
        }
        save_streams()
        context.user_data["step"] = "add_source"
        context.user_data["sid"] = sid
        await update.message.reply_text("📥 أرسل رابط المصدر (m3u8 أو مباشر):")
        return

    if context.user_data.get("step") == "add_source":
        sid = context.user_data["sid"]
        if sid in streams:
            streams[sid]["source"] = text
            save_streams()
            context.user_data.pop("step")
            context.user_data.pop("sid")
            # Ask for stream type
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("HLS (لمتصفحات/أجهزة)", callback_data=f"settype_{sid}_hls")],
                [InlineKeyboardButton("RTMP (للبث المباشر)", callback_data=f"settype_{sid}_rtmp")]
            ])
            await update.message.reply_text("اختر نوع البث:", reply_markup=kb)
        else:
            await update.message.reply_text("❌ خطأ")
        return

    # Editing existing stream values
    if context.user_data.get("edit"):
        typ, sid, edit_chat_id = context.user_data["edit"]
        if typ == "source":
            streams[sid]["source"] = text
            await update.message.reply_text("✅ تم تحديث المصدر")
        elif typ == "logo":
            streams[sid]["logo"] = "" if text == "/skip" else text
            await update.message.reply_text("✅ تم تحديث الشعار")
        elif typ == "ua":
            streams[sid]["ua"] = "ExoPlayerLib/2.18.5" if text == "/skip" else text
            await update.message.reply_text("✅ تم تحديث User-Agent")
        elif typ == "rename":
            old = streams[sid]["name"]
            streams[sid]["name"] = text
            await update.message.reply_text(f"✅ تم تغيير الاسم من {old} إلى {text}")
        elif typ == "rtmp_server":
            streams[sid]["rtmp_server"] = text
            await update.message.reply_text("✅ تم تحديث خادم RTMP")
        elif typ == "rtmp_key":
            streams[sid]["rtmp_key"] = text
            await update.message.reply_text("✅ تم تحديث مفتاح RTMP")
        save_streams()
        context.user_data.pop("edit")
        # Send updated panel
        await send_panel(edit_chat_id, sid, context.bot)
        return

    # Default: maybe stream name
    for sid, s in streams.items():
        if s["name"] == text:
            await send_panel(chat_id, sid, context.bot)
            return
    await update.message.reply_text("❌ اختر من الأزرار أو أرسل اسم بث موجود")

# =========================================
# CALLBACK FOR SETTING TYPE AFTER ADD
# =========================================
async def set_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, sid, typ = query.data.split("_")
    if sid in streams:
        streams[sid]["type"] = typ
        if typ == "rtmp":
            # Ask for RTMP server
            context.user_data["edit"] = ("rtmp_server", sid, query.message.chat_id)
            await query.edit_message_text("📡 أرسل خادم RTMP (مثال: rtmp://live.twitch.tv/app):")
        else:
            save_streams()
            await send_panel(query.message.chat_id, sid, context.bot)
    else:
        await query.edit_message_text("❌ خطأ")

# =========================================
# START COMMAND
# =========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return
    await update.message.reply_text(
        "🎬 **بوت البث المباشر - الإدارة الكاملة**\n\n"
        "📌 الأزرار السفلية للقوائم.\n"
        "➕ إضافة بث جديد.\n"
        "📺 HLS: بث لمتصفحات / أجهزة.\n"
        "📡 RTMP: بث لـ YouTube / Facebook / Twitch.\n"
        "⚙️ الترميز: جودة عالية (1080p, 9Mbps).\n\n"
        "استخدم الأزرار أدناه:",
        reply_markup=main_kb
    )

# =========================================
# MAIN
# =========================================
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_http())
    loop.create_task(clean_viewers_loop())

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(callback_handler, pattern="^(?!settype_).*"))
    app.add_handler(CallbackQueryHandler(set_type_callback, pattern="^settype_"))

    print("🚀 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()