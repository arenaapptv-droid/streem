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
# PSUTIL (optional)
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

# لتخزين معرف الرسالة الخاصة بكل بث
stream_message_ids = {}

# متغيرات المراقبة
monitor_active = False
monitor_task = None
monitor_chat_id = None
monitor_msg_id = None

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
# STREAM PANEL TEXT
# =========================================
def get_panel_text(sid):
    s = streams.get(sid)
    if not s:
        return "❌ البث غير موجود"
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
    return text

def stream_panel_keyboard(sid, s):
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
    kb.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

async def update_stream_message(sid, bot):
    s = streams.get(sid)
    if not s:
        return
    chat_id = s.get("chat_id")
    msg_id = s.get("message_id")
    if not chat_id or not msg_id:
        return
    try:
        text = get_panel_text(sid)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=stream_panel_keyboard(sid, s),
            parse_mode="Markdown"
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            print(f"Error updating message for {sid}: {e}")

# =========================================
# INLINE KEYBOARDS FOR LISTS
# =========================================
def streams_inline_keyboard(stream_type):
    kb = []
    for sid, s in streams.items():
        if s.get("type") == stream_type:
            status = "🟢" if s.get("active") else "🔴"
            kb.append([InlineKeyboardButton(f"{status} {s['name']}", callback_data=f"open_{sid}")])
    if not kb:
        kb.append([InlineKeyboardButton("❌ لا توجد بثوث", callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

# =========================================
# FFMPEG STREAM
# =========================================
async def start_stream(sid, bot):
    s = streams[sid]
    src = s["source"]
    mode = s.get("mode", "copy")
    ua = s.get("ua", "ExoPlayerLib/2.18.5")
    logo = s.get("logo", "")
    typ = s.get("type", "hls")

    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "index.m3u8")

    reconnect_opts = [
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
        "-timeout", "10000000", "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "1000000", "-probesize", "5000000"
    ]

    if mode == "copy":
        video_opts = ["-c:v", "copy"]
    else:
        video_opts = [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
            "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
            "-g", "90",
            "-vsync", "cfr", "-r", "30"
        ]

    audio_opts = ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]

    if typ == "hls":
        if logo and mode != "copy":
            cmd = [
                "ffmpeg", "-re", "-user_agent", ua,
                *reconnect_opts,
                "-i", src,
                "-i", logo,
                "-filter_complex",
                "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0",
                *video_opts, *audio_opts,
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", out_file
            ]
        else:
            cmd = [
                "ffmpeg", "-re", "-user_agent", ua,
                *reconnect_opts,
                "-i", src,
                *video_opts, *audio_opts,
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", out_file
            ]
    else:
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        if logo and mode != "copy":
            cmd = [
                "ffmpeg", "-re", "-user_agent", ua,
                *reconnect_opts,
                "-i", src,
                "-i", logo,
                "-filter_complex",
                "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0",
                *video_opts, *audio_opts,
                "-f", "flv", rtmp_url
            ]
        else:
            cmd = [
                "ffmpeg", "-re", "-user_agent", ua,
                *reconnect_opts,
                "-i", src,
                *video_opts, *audio_opts,
                "-f", "flv", rtmp_url
            ]

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
    await update_stream_message(sid, bot)

    async def stderr_reader():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            txt = line.decode(errors="ignore").strip()
            m = re.search(r"fps=\s*([\d.]+)", txt)
            if m:
                s["fps"] = m.group(1)
                await update_stream_message(sid, bot)
            if "error" in txt.lower():
                print(f"[{sid}] ffmpeg: {txt}")
    asyncio.create_task(stderr_reader())

    await proc.wait()
    s["active"] = False
    processes.pop(sid, None)
    save_streams()
    await update_stream_message(sid, bot)

async def stop_stream(sid, bot):
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
    await update_stream_message(sid, bot)

# =========================================
# مراقبة السيرفر (تحديث كل 3 ثوانٍ)
# =========================================
async def monitor_loop(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id, message_id):
    global monitor_active
    while monitor_active:
        status_text = system_status()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ إيقاف المراقبة", callback_data="stop_monitor")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
        ])
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=status_text,
                reply_markup=kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Monitor edit error: {e}")
        await asyncio.sleep(3)
    # عند الخروج من الحلقة (تم إيقاف المراقبة)، نرسل حالة ثابتة
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=system_status(),
            reply_markup=main_kb,
            parse_mode="Markdown"
        )
    except: pass

# =========================================
# CALLBACK HANDLER
# =========================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_active, monitor_task, monitor_chat_id, monitor_msg_id
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    message_id = query.message.message_id

    if data == "main_menu":
        if monitor_active:
            monitor_active = False
            if monitor_task:
                monitor_task.cancel()
        await query.edit_message_text("🎬 القائمة الرئيسية", reply_markup=main_kb)
        return

    if data == "stop_monitor":
        if monitor_active:
            monitor_active = False
            if monitor_task:
                monitor_task.cancel()
        await query.edit_message_text(system_status(), reply_markup=main_kb, parse_mode="Markdown")
        return

    if data == "list_hls":
        await query.edit_message_text("📺 بثوث HLS:", reply_markup=streams_inline_keyboard("hls"))
        return
    if data == "list_rtmp":
        await query.edit_message_text("📡 بثوث RTMP:", reply_markup=streams_inline_keyboard("rtmp"))
        return

    if data.startswith("open_"):
        sid = data[5:]
        if sid in streams:
            streams[sid]["chat_id"] = chat_id
            streams[sid]["message_id"] = message_id
            save_streams()
            text = get_panel_text(sid)
            await query.edit_message_text(
                text,
                reply_markup=stream_panel_keyboard(sid, streams[sid]),
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("❌ البث غير موجود")
        return

    if data.startswith("start_"):
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
            await query.answer("⏳ جاري التشغيل...")
            asyncio.create_task(start_stream(sid, context.bot))
        else:
            await query.answer("❌ خطأ")
        return

    if data.startswith("stop_"):
        sid = data[5:]
        await query.answer("⏹ تم الإيقاف")
        asyncio.create_task(stop_stream(sid, context.bot))
        return

    if data.startswith("source_"):
        sid = data[7:]
        context.user_data["edit"] = ("source", sid, chat_id, message_id)
        await query.edit_message_text("📥 أرسل رابط المصدر الجديد:")
        return

    if data.startswith("logo_"):
        sid = data[5:]
        context.user_data["edit"] = ("logo", sid, chat_id, message_id)
        await query.edit_message_text("🖼 أرسل رابط الشعار (أو /skip):")
        return

    if data.startswith("ua_"):
        sid = data[3:]
        context.user_data["edit"] = ("ua", sid, chat_id, message_id)
        await query.edit_message_text("🕵️ أرسل User-Agent (أو /skip):")
        return

    if data.startswith("rename_"):
        sid = data[7:]
        context.user_data["edit"] = ("rename", sid, chat_id, message_id)
        await query.edit_message_text("✏️ أرسل الاسم الجديد:")
        return

    if data.startswith("rtmpsrv_"):
        sid = data[8:]
        context.user_data["edit"] = ("rtmp_server", sid, chat_id, message_id)
        await query.edit_message_text("📡 أرسل خادم RTMP (مثال: rtmp://live.twitch.tv/app):")
        return

    if data.startswith("rtmpkey_"):
        sid = data[8:]
        context.user_data["edit"] = ("rtmp_key", sid, chat_id, message_id)
        await query.edit_message_text("🔑 أرسل مفتاح البث (stream key):")
        return

    if data.startswith("mode_"):
        sid = data[5:]
        if sid in streams:
            old = streams[sid]["mode"]
            streams[sid]["mode"] = "encode" if old == "copy" else "copy"
            save_streams()
            await query.answer(f"✅ تم التبديل إلى {'ترميز' if streams[sid]['mode']=='encode' else 'نسخ'}")
            if streams[sid].get("active"):
                await stop_stream(sid, context.bot)
                asyncio.create_task(start_stream(sid, context.bot))
            else:
                await update_stream_message(sid, context.bot)
        else:
            await query.answer("❌ خطأ")
        return

    if data.startswith("del_"):
        sid = data[4:]
        await stop_stream(sid, context.bot)
        if sid in streams:
            streams.pop(sid, None)
            save_streams()
        await query.edit_message_text(
            "🗑 تم حذف البث",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")]])
        )
        return

    await query.answer("لا شيء")

# =========================================
# TEXT MESSAGE HANDLER
# =========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_active, monitor_task, monitor_chat_id, monitor_msg_id
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return

    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "📺 قائمة HLS":
        await update.message.reply_text("📺 بثوث HLS:", reply_markup=streams_inline_keyboard("hls"))
        return
    if text == "📡 قائمة RTMP":
        await update.message.reply_text("📡 بثوث RTMP:", reply_markup=streams_inline_keyboard("rtmp"))
        return
    if text == "➕ إضافة بث":
        context.user_data["step"] = "add_name"
        await update.message.reply_text("📝 أرسل اسم البث الجديد:")
        return
    if text == "🖥 مراقبة السيرفر":
        # إذا كانت المراقبة نشطة، نوقفها
        if monitor_active:
            monitor_active = False
            if monitor_task:
                monitor_task.cancel()
            try:
                await context.bot.edit_message_text(
                    chat_id=monitor_chat_id,
                    message_id=monitor_msg_id,
                    text=system_status(),
                    reply_markup=main_kb,
                    parse_mode="Markdown"
                )
            except:
                pass
            return
        # بدء مراقبة جديدة
        msg = await update.message.reply_text("⏳ جاري تحميل حالة السيرفر...")
        monitor_chat_id = msg.chat_id
        monitor_msg_id = msg.message_id
        monitor_active = True
        monitor_task = asyncio.create_task(monitor_loop(update, context, monitor_chat_id, monitor_msg_id))
        return
    if text == "🧹 تنظيف الملفات":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        await update.message.reply_text("✅ تم تنظيف مجلد HLS")
        return

    # إضافة بث جديد
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
            "rtmp_key": "",
            "chat_id": None,
            "message_id": None
        }
        save_streams()
        context.user_data["step"] = "add_source"
        context.user_data["sid"] = sid
        await update.message.reply_text("📥 أرسل رابط المصدر (m3u8 أو مباشر):")
        return

    if context.user_data.get("step") == "add_source":
        sid = context.user_data.get("sid")
        if sid and sid in streams:
            streams[sid]["source"] = text
            save_streams()
            context.user_data.pop("step")
            context.user_data.pop("sid")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("HLS (لمتصفحات/أجهزة)", callback_data=f"settype_{sid}_hls")],
                [InlineKeyboardButton("RTMP (للبث المباشر)", callback_data=f"settype_{sid}_rtmp")]
            ])
            await update.message.reply_text("اختر نوع البث:", reply_markup=kb)
        else:
            await update.message.reply_text("❌ خطأ")
        return

    # تعديل البيانات
    if context.user_data.get("edit"):
        typ, sid, edit_chat_id, edit_msg_id = context.user_data["edit"]
        if typ == "source":
            streams[sid]["source"] = text
        elif typ == "logo":
            streams[sid]["logo"] = "" if text == "/skip" else text
        elif typ == "ua":
            streams[sid]["ua"] = "ExoPlayerLib/2.18.5" if text == "/skip" else text
        elif typ == "rename":
            old = streams[sid]["name"]
            streams[sid]["name"] = text
            await update.message.reply_text(f"✅ تم تغيير الاسم من {old} إلى {text}")
        elif typ == "rtmp_server":
            streams[sid]["rtmp_server"] = text
        elif typ == "rtmp_key":
            streams[sid]["rtmp_key"] = text
        save_streams()
        context.user_data.pop("edit")
        await update_stream_message(sid, context.bot)
        await update.message.delete()
        return

    await update.message.reply_text("❌ اختر من الأزرار")

# =========================================
# SET TYPE CALLBACK
# =========================================
async def set_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, sid, typ = query.data.split("_")
    if sid in streams:
        streams[sid]["type"] = typ
        if typ == "rtmp":
            context.user_data["edit"] = ("rtmp_server", sid, query.message.chat_id, query.message.message_id)
            await query.edit_message_text("📡 أرسل خادم RTMP (مثال: rtmp://live.twitch.tv/app):")
        else:
            save_streams()
            text = get_panel_text(sid)
            await query.edit_message_text(
                text,
                reply_markup=stream_panel_keyboard(sid, streams[sid]),
                parse_mode="Markdown"
            )
    else:
        await query.edit_message_text("❌ خطأ")

# =========================================
# MAIN KEYBOARD
# =========================================
main_kb = ReplyKeyboardMarkup([
    ["📺 قائمة HLS", "📡 قائمة RTMP"],
    ["➕ إضافة بث", "🖥 مراقبة السيرفر"],
    ["🧹 تنظيف الملفات"]
], resize_keyboard=True)

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
        "📡 RTMP: بث لـ YouTube / Facebook / Twitch.\n\n"
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