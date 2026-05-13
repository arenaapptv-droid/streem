import asyncio, time, json, os, logging, re, shutil, traceback
from collections import defaultdict
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ========== إعدادات السجل (تقليل الإزعاج) ==========
logging.basicConfig(
    level=logging.WARNING,  # إظهار التحذيرات والأخطاء فقط
    format="%(asctime)s [%(levelname)s] %(message)s"
)
# إسكات سجلات httpx و aiohttp.access المزعجة
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

logger = logging.getLogger("RplayStable")

# ========== الإعدادات ==========
with open("settings.json", "r") as f:
    settings = json.load(f)
    TOKEN = settings["TOKEN"]
    ADMIN_ID = settings["ADMIN_ID"]

HLS_DIR = "/tmp/hls"
os.makedirs(HLS_DIR, exist_ok=True)

HTTP_PORT = 8080
BASE_URL = "http://164.68.102.28"

STREAMS_FILE = "streams_pro.json"
streams = {}

if os.path.exists(STREAMS_FILE):
    try:
        with open(STREAMS_FILE) as f:
            loaded = json.load(f)
            for sid, s_data in loaded.items():
                if "viewers" in s_data and isinstance(s_data["viewers"], list):
                    s_data["viewers"] = set(s_data["viewers"])
                s_data.setdefault("type", "hls")
                s_data.setdefault("user_agent", "")
                s_data.setdefault("uptime", "00:00:00")
                s_data.setdefault("last_fps", "?")
                s_data.setdefault("start_time", 0)
                s_data.setdefault("panel_msg_id", None)
                s_data.setdefault("panel_chat_id", None)
                s_data.setdefault("mode", "transcode")
                s_data.setdefault("rtmp_server", "")
                s_data.setdefault("rtmp_key", "")
                streams[sid] = s_data
    except (json.JSONDecodeError, KeyError):
        logger.error("Failed to parse streams_pro.json, starting fresh.")
        streams = {}

def save_streams():
    data = {}
    for sid, s in streams.items():
        s_copy = s.copy()
        if "viewers" in s_copy and isinstance(s_copy["viewers"], set):
            s_copy["viewers"] = list(s_copy["viewers"])
        s_copy.pop("process", None)
        data[sid] = s_copy
    with open(STREAMS_FILE, "w") as f:
        json.dump(data, f, indent=2)

viewer_last_seen = defaultdict(dict)

async def track_viewer(request, stream_name):
    ip = request.remote
    now = time.time()
    if stream_name in streams and streams[stream_name].get("type") == "hls":
        s = streams[stream_name]
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
            if now - viewer_last_seen[sid].get(ip, 0) > 10:
                s["viewers"].discard(ip)
                if ip in viewer_last_seen[sid]:
                    del viewer_last_seen[sid][ip]

async def handle_hls(request):
    name = request.match_info["name"]
    file = request.match_info.get("file", "index.m3u8")
    path = os.path.join(HLS_DIR, name, file)
    if not os.path.exists(path):
        return web.Response(status=404)
    await track_viewer(request, name)
    if path.endswith(".m3u8"):
        return web.FileResponse(path, headers={"Content-Type": "application/vnd.apple.mpegurl"})
    elif path.endswith(".ts"):
        return web.FileResponse(path, headers={"Content-Type": "video/mp2t"})
    return web.FileResponse(path)

async def start_http_server():
    app = web.Application()
    app.router.add_get("/live/{name}/{file:.*}", handle_hls)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()

def get_system_status():
    cpu = 0.0
    try:
        with open("/proc/stat") as f:
            line = f.readline().split()
            if line[0] == "cpu":
                idle1, total1 = int(line[4]), sum(map(int, line[1:5]))
        time.sleep(0.1)
        with open("/proc/stat") as f:
            line = f.readline().split()
            if line[0] == "cpu":
                idle2, total2 = int(line[4]), sum(map(int, line[1:5]))
        if total2 - total1 > 0:
            cpu = 100 * (1 - (idle2 - idle1) / (total2 - total1))
    except: pass
    ram = "N/A"
    try:
        with open("/proc/meminfo") as f:
            l = f.readlines()
            total = int(l[0].split()[1]) // 1024
            avail = int([x for x in l if "MemAvailable" in x][0].split()[1]) // 1024
            ram = f"{total - avail} / {total} MiB"
    except: pass
    return f"🖥 CPU: {cpu:.1f}% | RAM: {ram}"

monitor_tasks = {}

async def start_monitor_live(query, chat_id, message_id):
    if chat_id in monitor_tasks:
        monitor_tasks[chat_id].cancel()
    async def update_loop():
        try:
            while True:
                status = get_system_status()
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏹ إيقاف المراقبة", callback_data="stop_monitor")],
                    [InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")]
                ])
                try:
                    await query.edit_message_text(status, reply_markup=kb)
                except:
                    pass
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
    task = asyncio.create_task(update_loop())
    monitor_tasks[chat_id] = task

async def stop_monitor(chat_id, query):
    if chat_id in monitor_tasks:
        monitor_tasks[chat_id].cancel()
        del monitor_tasks[chat_id]
    status = get_system_status()
    await query.edit_message_text(status, reply_markup=main_menu())

async def check_admin(update):
    if update.effective_user.id != ADMIN_ID:
        if update.message: await update.message.reply_text("🚫 غير مصرح")
        elif update.callback_query: await update.callback_query.answer("🚫 غير مصرح", show_alert=True)
        return False
    return True

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📺 HLS"), KeyboardButton("📡 RTMP")],
        [KeyboardButton("➕ إضافة"), KeyboardButton("🖥 مراقبة")]
    ],
    resize_keyboard=True
)

def main_menu():
    kb = [
        [InlineKeyboardButton("📺 بثوث HLS", callback_data="list_hls")],
        [InlineKeyboardButton("📡 بثوث RTMP", callback_data="list_rtmp")],
        [InlineKeyboardButton("➕ إضافة بث", callback_data="add_stream")],
        [InlineKeyboardButton("🖥 مراقبة السيرفر", callback_data="monitor")],
    ]
    return InlineKeyboardMarkup(kb)

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
        name = s.get("name", sid)
        status = "🟢" if s.get("active") else "⏹"
        kb.append([InlineKeyboardButton(f"{status} {name}", callback_data=f"panel_{sid}")])
    kb.append(navigation_row())
    return InlineKeyboardMarkup(kb)

def stream_panel_keyboard(sid, s):
    stream_type = s.get("type", "hls")
    mode = s.get("mode", "transcode")
    kb = []
    if s.get("active"):
        kb.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{sid}")])
    else:
        kb.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{sid}")])
    kb.append([
        InlineKeyboardButton("📥 مصدر", callback_data=f"source_{sid}"),
        InlineKeyboardButton("🖼 شعار", callback_data=f"logo_{sid}")
    ])
    kb.append([
        InlineKeyboardButton("🕵️ UA", callback_data=f"ua_{sid}"),
        InlineKeyboardButton("✏️ إعادة تسمية", callback_data=f"rename_{sid}")
    ])
    if stream_type == "rtmp":
        kb.append([
            InlineKeyboardButton("📡 خادم RTMP", callback_data=f"rtmpsrv_{sid}"),
            InlineKeyboardButton("🔑 مفتاح RTMP", callback_data=f"rtmpkey_{sid}")
        ])
    if mode == "transcode":
        toggle_text = "🔄 نسخ مباشر (أخف)"
    else:
        toggle_text = "⚙️ ترميز (شعار)"
    kb.append([InlineKeyboardButton(toggle_text, callback_data=f"togglemode_{sid}")])
    kb.append([InlineKeyboardButton("🗑 حذف", callback_data=f"delete_{sid}")])
    if s.get("active"):
        source_status = "🟢" if s.get("source_online") else "🔴"
        viewers = len(s.get("viewers", set())) if stream_type == "hls" else 0
        uptime = s.get('uptime', '00:00:00')
        mode_label = "نسخ" if mode == "copy" else "ترميز"
        info = f"{source_status} FPS:{s.get('last_fps','?')} | ⏱️{uptime} | 👥{viewers} | {mode_label}"
        kb.append([InlineKeyboardButton(info, callback_data="noop")])
    kb.append(navigation_row())
    return InlineKeyboardMarkup(kb)

async def start(update, context):
    if not await check_admin(update): return
    await update.message.reply_text("🖥 **Rplay HLS & RTMP**", reply_markup=main_menu())
    await update.message.reply_text("اختر من الأزرار السفلية للتنقل السريع:", reply_markup=MAIN_KEYBOARD)

async def button_handler(update, context):
    q = update.callback_query
    await q.answer()
    d = q.data
    if not await check_admin(update): return
    chat_id = q.message.chat_id

    if d == "monitor":
        await start_monitor_live(q, chat_id, q.message.message_id)
        return
    if d == "stop_monitor":
        await stop_monitor(chat_id, q)
        return

    if d == "main_menu":
        await q.edit_message_text("🖥 **Rplay HLS & RTMP**", reply_markup=main_menu())
    elif d in ("list_hls", "list_rtmp"):
        stream_type = "hls" if d == "list_hls" else "rtmp"
        await q.edit_message_text(f"📋 **بثوث {stream_type.upper()}**", reply_markup=stream_list(stream_type))
    elif d == "add_stream":
        context.user_data["mode"] = "add_stream_name"
        await q.edit_message_text("📝 أرسل اسم البث الجديد:")
    elif d.startswith("panel_"):
        sid = d.split("_", 1)[1]
        s = streams.get(sid)
        if not s:
            await q.edit_message_text("❌ البث غير موجود", reply_markup=main_menu())
            return
        s["panel_msg_id"] = q.message.message_id
        s["panel_chat_id"] = q.message.chat_id
        name = s.get("name", sid)
        stream_type = s.get("type", "hls")
        rtmp_info = ""
        if stream_type == "rtmp":
            rtmp_info = f"\n📡 RTMP: {s.get('rtmp_server', 'غير محدد')}/{s.get('rtmp_key', 'غير محدد')}"
        link = ""
        if stream_type == "hls":
            link = f"\n🔗 الرابط: {BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8"
        await q.edit_message_text(
            f"🎛️ **{name}** ({stream_type.upper()})\n"
            f"📥 المصدر: {s['source'] or 'غير محدد'}\n"
            f"🖼 الشعار: {'موجود' if s['logo'] else 'لا يوجد'}\n"
            f"🕵️ UA: {s['user_agent'] or 'افتراضي'}"
            f"{rtmp_info}"
            f"{link}",
            reply_markup=stream_panel_keyboard(sid, s)
        )
    elif "_" in d:
        act, sid = d.split("_", 1)
        if act in ("list_hls", "list_rtmp"):
            return
        s = streams.get(sid)
        if not s:
            await q.edit_message_text("❌ البث غير موجود", reply_markup=main_menu())
            return
        name = s.get("name", sid)

        if act == "start":
            if not s["source"]:
                await q.answer("❌ عيّن المصدر أولاً", show_alert=True)
                return
            if s.get("type") == "rtmp" and (not s.get("rtmp_server") or not s.get("rtmp_key")):
                await q.answer("❌ عيّن خادم ومفتاح RTMP أولاً", show_alert=True)
                return
            asyncio.create_task(start_stream(sid, context.bot))
            await q.answer("⏳ جاري التشغيل...")
        elif act == "stop":
            await stop_stream(sid, context.bot)
            await q.answer("⏹ تم الإيقاف والتنظيف")
        elif act == "source":
            context.user_data["mode"] = f"source_{sid}"
            await q.edit_message_text(f"📥 أرسل رابط المصدر لـ {name}:")
            return
        elif act == "logo":
            context.user_data["mode"] = f"logo_{sid}"
            await q.edit_message_text(f"🖼 أرسل رابط الشعار لـ {name} (أو /skip):")
            return
        elif act == "ua":
            context.user_data["mode"] = f"ua_{sid}"
            await q.edit_message_text(f"🕵️ أرسل User-Agent لـ {name} (أو /skip):")
            return
        elif act == "rename":
            context.user_data["mode"] = f"rename_{sid}"
            await q.edit_message_text(f"✏️ أرسل الاسم الجديد لـ {name}:")
            return
        elif act == "rtmpsrv":
            context.user_data["mode"] = f"rtmpsrv_{sid}"
            await q.edit_message_text(f"📡 أرسل رابط خادم RTMP لـ {name} (مثال: rtmp://example.com/live):")
            return
        elif act == "rtmpkey":
            context.user_data["mode"] = f"rtmpkey_{sid}"
            await q.edit_message_text(f"🔑 أرسل مفتاح RTMP لـ {name}:")
            return
        elif act == "delete":
            await stop_stream(sid, context.bot)
            del streams[sid]
            save_streams()
            await q.edit_message_text(f"🗑 تم حذف {name} بالكامل", reply_markup=main_menu())
            return
        elif act == "togglemode":
            old_mode = s.get("mode", "transcode")
            new_mode = "copy" if old_mode == "transcode" else "transcode"
            s["mode"] = new_mode
            save_streams()
            if s.get("active"):
                await stop_stream(sid, context.bot)
                asyncio.create_task(start_stream(sid, context.bot))
                await q.answer(f"⏳ جاري التشغيل بوضع {'نسخ' if new_mode == 'copy' else 'ترميز'}...")
            else:
                await q.answer(f"✅ تم التبديل إلى وضع {'نسخ' if new_mode == 'copy' else 'ترميز'}")
            stream_type = s.get("type", "hls")
            rtmp_info = ""
            if stream_type == "rtmp":
                rtmp_info = f"\n📡 RTMP: {s.get('rtmp_server', 'غير محدد')}/{s.get('rtmp_key', 'غير محدد')}"
            link = ""
            if stream_type == "hls":
                link = f"\n🔗 الرابط: {BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8"
            await q.edit_message_text(
                f"🎛️ **{name}** ({stream_type.upper()})\n"
                f"📥 المصدر: {s['source'] or 'غير محدد'}\n"
                f"🖼 الشعار: {'موجود' if s['logo'] else 'لا يوجد'}\n"
                f"🕵️ UA: {s['user_agent'] or 'افتراضي'}"
                f"{rtmp_info}"
                f"{link}",
                reply_markup=stream_panel_keyboard(sid, s)
            )
            return

        stream_type = s.get("type", "hls")
        rtmp_info = ""
        if stream_type == "rtmp":
            rtmp_info = f"\n📡 RTMP: {s.get('rtmp_server', 'غير محدد')}/{s.get('rtmp_key', 'غير محدد')}"
        link = ""
        if stream_type == "hls":
            link = f"\n🔗 الرابط: {BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8"
        await q.edit_message_text(
            f"🎛️ **{name}** ({stream_type.upper()})\n"
            f"📥 المصدر: {s['source'] or 'غير محدد'}\n"
            f"🖼 الشعار: {'موجود' if s['logo'] else 'لا يوجد'}\n"
            f"🕵️ UA: {s['user_agent'] or 'افتراضي'}"
            f"{rtmp_info}"
            f"{link}",
            reply_markup=stream_panel_keyboard(sid, s)
        )

async def msg_handler(update, context):
    if not await check_admin(update): return
    text = update.message.text.strip()

    if text == "📺 HLS":
        await update.message.reply_text("📋 **بثوث HLS**", reply_markup=stream_list("hls"))
        return
    elif text == "📡 RTMP":
        await update.message.reply_text("📋 **بثوث RTMP**", reply_markup=stream_list("rtmp"))
        return
    elif text == "➕ إضافة":
        context.user_data["mode"] = "add_stream_name"
        await update.message.reply_text("📝 أرسل اسم البث الجديد:")
        return
    elif text == "🖥 مراقبة":
        msg = await update.message.reply_text("⏳ جاري تحميل حالة السيرفر...")
        await start_monitor_live(None, update.message.chat_id, msg.message_id)
        return

    mode = context.user_data.get("mode")

    if mode == "add_stream_name":
        context.user_data["mode"] = "add_stream_type"
        context.user_data["new_name"] = text
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📺 HLS", callback_data="newtype_hls"),
             InlineKeyboardButton("📡 RTMP", callback_data="newtype_rtmp")]
        ])
        await update.message.reply_text(f"اختر نوع البث **{text}**:", reply_markup=kb)
        return

    if mode and "_" in mode:
        act, sid = mode.split("_", 1)
        context.user_data["mode"] = None
        s = streams.get(sid)
        if not s:
            await update.message.reply_text("❌ بث غير موجود")
            return

        if act == "source":
            s["source"] = text
        elif act == "logo":
            s["logo"] = "" if text.lower() == "/skip" else text
        elif act == "ua":
            s["user_agent"] = "" if text.lower() == "/skip" else text
        elif act == "rename":
            old = s["name"]
            s["name"] = text
            await update.message.reply_text(f"✅ تم تغيير الاسم من {old} إلى {text}")
            save_streams()
            await update_panel_message(sid, context.bot)
            return
        elif act == "rtmpsrv":
            s["rtmp_server"] = text
        elif act == "rtmpkey":
            s["rtmp_key"] = text

        save_streams()
        await update.message.reply_text(f"✅ تم حفظ {act} لـ {s['name']}")
        await update_panel_message(sid, context.bot)

async def update_panel_message(sid, bot):
    s = streams.get(sid)
    if not s or not s.get("panel_msg_id") or not s.get("panel_chat_id"):
        return
    try:
        name = s.get("name", sid)
        stream_type = s.get("type", "hls")
        source_status = "🟢" if s.get("source_online") else "🔴"
        viewers = len(s.get("viewers", set())) if stream_type == "hls" else 0
        uptime = s.get('uptime', '00:00:00')
        mode_label = "نسخ" if s.get("mode") == "copy" else "ترميز"
        rtmp_info = ""
        if stream_type == "rtmp":
            rtmp_info = f"\n📡 RTMP: {s.get('rtmp_server', 'غير محدد')}/{s.get('rtmp_key', 'غير محدد')}"
        link = ""
        if stream_type == "hls":
            link = f"\n🔗 الرابط: {BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8"
        text = (
            f"🎛️ **{name}** ({stream_type.upper()})\n"
            f"📥 المصدر: {s['source'] or 'غير محدد'}\n"
            f"🖼 الشعار: {'موجود' if s['logo'] else 'لا يوجد'}\n"
            f"🕵️ UA: {s['user_agent'] or 'افتراضي'}"
            f"{rtmp_info}"
            f"{link}"
        )
        await bot.edit_message_text(
            chat_id=s["panel_chat_id"],
            message_id=s["panel_msg_id"],
            text=text,
            reply_markup=stream_panel_keyboard(sid, s)
        )
    except Exception as e:
        logger.error(f"Update panel error: {e}")

async def start_stream(sid, bot):
    s = streams[sid]
    src = s["source"]
    logo = s.get("logo", "")
    user_agent = s.get("user_agent", "ExoPlayerLib/2.18.5")
    mode = s.get("mode", "transcode")
    stream_type = s.get("type", "hls")

    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_playlist = os.path.join(out_dir, "index.m3u8")

    if stream_type == "rtmp":
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        if mode == "copy":
            cmd = [
                "ffmpeg", "-re",
                "-user_agent", user_agent,
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "10000000", "-fflags", "+genpts+discardcorrupt",
                "-i", src,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-f", "flv", rtmp_url
            ]
        else:
            cmd = [
                "ffmpeg", "-re",
                "-user_agent", user_agent,
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "10000000", "-fflags", "+genpts+discardcorrupt",
                "-i", src
            ]
            if logo:
                cmd += [
                    "-i", logo,
                    "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0"
                ]
            cmd += [
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-b:v", "3000k", "-maxrate", "3000k", "-bufsize", "6000k",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-f", "flv", rtmp_url
            ]
        fallback_cmd = None
    else:
        if mode == "copy":
            cmd = [
                "ffmpeg", "-re",
                "-user_agent", user_agent,
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "10000000", "-fflags", "+genpts+discardcorrupt",
                "-i", src,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments",
                out_playlist
            ]
        else:
            cmd = [
                "ffmpeg", "-re",
                "-user_agent", user_agent,
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "10000000", "-fflags", "+genpts+discardcorrupt",
                "-i", src
            ]
            if logo:
                cmd += [
                    "-i", logo,
                    "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0"
                ]
            cmd += [
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-b:v", "3000k", "-maxrate", "3000k", "-bufsize", "6000k",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments",
                out_playlist
            ]
        fallback_cmd = [
            "ffmpeg", "-re",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30",
            "-i", logo if logo else "color=c=black:s=1920x1080:r=30",
            "-filter_complex", "[0:v][1:v]overlay=0:0" if logo else "null",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_playlist
        ]

    s["active"] = True
    s["start_time"] = time.time()
    save_streams()

    retries = 3
    while s["active"]:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        s["process"] = proc
        s["source_online"] = True

        async def read_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line: break
                decoded = line.decode(errors="ignore").strip()
                if "fps=" in decoded:
                    m = re.search(r"fps=\s*([\d.]+)", decoded)
                    if m: s["last_fps"] = m.group(1)

        reader = asyncio.create_task(read_stderr())

        while proc.returncode is None:
            clean_viewers()
            s["uptime"] = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
            await update_panel_message(sid, bot)
            await asyncio.sleep(5)

        await proc.wait()
        s["source_online"] = False
        await update_panel_message(sid, bot)

        retries -= 1
        if retries > 0:
            await asyncio.sleep(2)
            continue

        if s["active"] and fallback_cmd:
            fallback_proc = await asyncio.create_subprocess_exec(*fallback_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            s["process"] = fallback_proc
            s["fallback"] = True
            s["source_online"] = False
            await update_panel_message(sid, bot)
            await asyncio.sleep(20)
            fallback_proc.kill()
            await fallback_proc.wait()
            retries = 3
        else:
            break

    if s.get("process"):
        s["process"].kill()
    s["active"] = False
    save_streams()
    await update_panel_message(sid, bot)

async def stop_stream(sid, bot):
    s = streams.get(sid)
    if not s: return
    if s.get("process"):
        try:
            s["process"].kill()
            await s["process"].wait()
        except: pass
        s["process"] = None
    s["active"] = False
    s["source_online"] = False
    s["fallback"] = False
    stream_hls_dir = os.path.join(HLS_DIR, sid)
    if os.path.exists(stream_hls_dir):
        try:
            shutil.rmtree(stream_hls_dir)
        except: pass
    if isinstance(s.get("viewers"), set):
        s["viewers"].clear()
    s["last_fps"] = "?"
    s["uptime"] = "00:00:00"
    s["start_time"] = 0
    save_streams()
    await update_panel_message(sid, bot)

async def newtype_callback(update, context):
    q = update.callback_query
    await q.answer()
    d = q.data
    name = context.user_data.get("new_name", "بث")
    context.user_data["mode"] = None

    if d == "newtype_hls":
        stream_type = "hls"
    else:
        stream_type = "rtmp"

    raw_name = name.replace(' ', '_')
    sid = raw_name if raw_name not in streams else f"{raw_name}_{int(time.time())}"
    streams[sid] = {
        "name": name, "source": "", "logo": "", "user_agent": "",
        "active": False, "process": None, "fallback": False,
        "source_online": False, "viewers": set(), "last_fps": "?",
        "uptime": "00:00:00", "start_time": 0,
        "panel_msg_id": None, "panel_chat_id": None,
        "mode": "transcode", "rtmp_server": "", "rtmp_key": "",
        "type": stream_type
    }
    save_streams()
    await q.edit_message_text(f"✅ تم إضافة بث {stream_type.upper()} **{name}**", reply_markup=main_menu())

async def extra_button_handler(update, context):
    q = update.callback_query
    d = q.data
    if d.startswith("newtype_"):
        await newtype_callback(update, context)
    else:
        await button_handler(update, context)

# ========== معالج الأخطاء العام ==========
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تسجيل أي خطأ غير معالج لتجنب التوقف الصامت"""
    tb_str = ''.join(traceback.format_exception(None, context.error, context.error.__traceback__))
    logger.error(f"Update {update} caused error: {context.error}\n{tb_str}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_http_server())

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(extra_button_handler, pattern="^newtype_"))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    app.add_error_handler(error_handler)  # مهم جداً
    logger.info("Rplay Final Silent Ready")
    app.run_polling()