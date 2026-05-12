import asyncio, time, json, os, logging, re, shutil
from collections import defaultdict
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

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
                s_data.setdefault("user_agent", "")
                s_data.setdefault("uptime", "00:00:00")
                s_data.setdefault("last_fps", "?")
                s_data.setdefault("start_time", 0)
                s_data.setdefault("panel_msg_id", None)
                s_data.setdefault("panel_chat_id", None)
                s_data.setdefault("mode", "transcode")   # وضع افتراضي
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RplayFinal")

viewer_last_seen = defaultdict(dict)

async def track_viewer(request, stream_name):
    ip = request.remote
    now = time.time()
    if stream_name in streams:
        s = streams[stream_name]
        if not isinstance(s.get("viewers"), set):
            s["viewers"] = set()
        s["viewers"].add(ip)
        viewer_last_seen[stream_name][ip] = now

def clean_viewers():
    now = time.time()
    for sid, s in streams.items():
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
    logger.info(f"HTTP server on port {HTTP_PORT}")

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

def main_menu():
    kb = []
    for sid, s in streams.items():
        name = s.get("name", sid)
        status = "🟢" if s.get("active") else "⏹"
        kb.append([InlineKeyboardButton(f"{status} {name}", callback_data=f"panel_{sid}")])
    kb.append([InlineKeyboardButton("➕ إضافة بث", callback_data="add_stream")])
    kb.append([InlineKeyboardButton("🖥 مراقبة السيرفر", callback_data="monitor")])
    return InlineKeyboardMarkup(kb)

def stream_panel_keyboard(sid, s):
    name = s.get("name", sid)
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
    # زر تبديل الوضع
    if mode == "transcode":
        toggle_text = "🔄 نسخ مباشر"
        toggle_data = f"togglemode_{sid}"
    else:
        toggle_text = "⚙️ ترميز"
        toggle_data = f"togglemode_{sid}"
    kb.append([InlineKeyboardButton(toggle_text, callback_data=toggle_data)])
    kb.append([InlineKeyboardButton("🗑 حذف", callback_data=f"delete_{sid}")])
    if s.get("active"):
        source_status = "🟢" if s.get("source_online") else "🔴"
        viewers = len(s.get("viewers", set()))
        uptime = s.get('uptime', '00:00:00')
        mode_label = "نسخ" if mode == "copy" else "ترميز"
        info = f"{source_status} FPS:{s.get('last_fps','?')} | ⏱️{uptime} | 👥{viewers} | {mode_label}"
        kb.append([InlineKeyboardButton(info, callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

async def start(update, context):
    if not await check_admin(update): return
    await update.message.reply_text("🖥 **Rplay Dynamic**", reply_markup=main_menu())

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
        await q.edit_message_text("🖥 **Rplay Dynamic**", reply_markup=main_menu())
    elif d == "add_stream":
        context.user_data["mode"] = "add_stream"
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
        await q.edit_message_text(
            f"🎛️ **{name}**\n"
            f"📥 المصدر: {s['source'] or 'غير محدد'}\n"
            f"🖼 الشعار: {'موجود' if s['logo'] else 'لا يوجد'}\n"
            f"🕵️ UA: {s['user_agent'] or 'افتراضي'}\n"
            f"🔗 الرابط: {BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8",
            reply_markup=stream_panel_keyboard(sid, s)
        )
    elif "_" in d:
        act, sid = d.split("_", 1)
        s = streams.get(sid)
        if not s:
            await q.edit_message_text("❌ البث غير موجود", reply_markup=main_menu())
            return
        name = s.get("name", sid)

        if act == "start":
            if not s["source"]:
                await q.answer("❌ عيّن المصدر أولاً", show_alert=True)
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
        elif act == "delete":
            await stop_stream(sid, context.bot)
            del streams[sid]
            save_streams()
            await q.edit_message_text(f"🗑 تم حذف {name} بالكامل", reply_markup=main_menu())
            return
        elif act == "togglemode":
            # تبديل الوضع
            old_mode = s.get("mode", "transcode")
            new_mode = "copy" if old_mode == "transcode" else "transcode"
            s["mode"] = new_mode
            save_streams()
            # إذا كان البث قيد التشغيل، أعد تشغيله بالوضع الجديد
            if s.get("active"):
                await stop_stream(sid, context.bot)
                asyncio.create_task(start_stream(sid, context.bot))
                await q.answer(f"⏳ جاري التشغيل بوضع {'نسخ' if new_mode == 'copy' else 'ترميز'}...")
            else:
                await q.answer(f"✅ تم التبديل إلى وضع {'نسخ' if new_mode == 'copy' else 'ترميز'}")
            # تحديث اللوحة
            await q.edit_message_text(
                f"🎛️ **{name}**\n"
                f"📥 المصدر: {s['source'] or 'غير محدد'}\n"
                f"🖼 الشعار: {'موجود' if s['logo'] else 'لا يوجد'}\n"
                f"🕵️ UA: {s['user_agent'] or 'افتراضي'}\n"
                f"🔗 الرابط: {BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8",
                reply_markup=stream_panel_keyboard(sid, s)
            )
            return

        # تحديث اللوحة بعد أي إجراء
        await q.edit_message_text(
            f"🎛️ **{name}**\n"
            f"📥 المصدر: {s['source'] or 'غير محدد'}\n"
            f"🖼 الشعار: {'موجود' if s['logo'] else 'لا يوجد'}\n"
            f"🕵️ UA: {s['user_agent'] or 'افتراضي'}\n"
            f"🔗 الرابط: {BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8",
            reply_markup=stream_panel_keyboard(sid, s)
        )

async def msg_handler(update, context):
    if not await check_admin(update): return
    text = update.message.text.strip()
    mode = context.user_data.get("mode")

    if mode == "add_stream":
        context.user_data["mode"] = None
        raw_name = text.replace(' ', '_')
        sid = raw_name if raw_name not in streams else f"{raw_name}_{int(time.time())}"
        streams[sid] = {
            "name": text, "source": "", "logo": "", "user_agent": "",
            "active": False, "process": None, "fallback": False,
            "source_online": False, "viewers": set(), "last_fps": "?",
            "uptime": "00:00:00", "start_time": 0,
            "panel_msg_id": None, "panel_chat_id": None,
            "mode": "transcode"
        }
        save_streams()
        await update.message.reply_text(f"✅ تم إضافة البث **{text}**\nاستخدم الأزرار لإعداده.", reply_markup=main_menu())
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
            save_streams()
            await update.message.reply_text(f"✅ تم حفظ المصدر لـ {s['name']}")
        elif act == "logo":
            s["logo"] = "" if text.lower() == "/skip" else text
            save_streams()
            await update.message.reply_text(f"✅ تم تحديث الشعار لـ {s['name']}")
        elif act == "ua":
            s["user_agent"] = "" if text.lower() == "/skip" else text
            save_streams()
            await update.message.reply_text(f"✅ تم تحديث User-Agent لـ {s['name']}")
        elif act == "rename":
            old = s["name"]
            s["name"] = text
            save_streams()
            await update.message.reply_text(f"✅ تم تغيير الاسم من {old} إلى {text}")

async def update_panel_message(sid, bot):
    s = streams.get(sid)
    if not s or not s.get("panel_msg_id") or not s.get("panel_chat_id"):
        return
    try:
        name = s.get("name", sid)
        source_status = "🟢" if s.get("source_online") else "🔴"
        viewers = len(s.get("viewers", set()))
        uptime = s.get('uptime', '00:00:00')
        mode_label = "نسخ" if s.get("mode") == "copy" else "ترميز"
        info = f"{source_status} FPS:{s.get('last_fps','?')} | ⏱️{uptime} | 👥{viewers} | {mode_label}"
        text = (
            f"🎛️ **{name}**\n"
            f"📥 المصدر: {s['source'] or 'غير محدد'}\n"
            f"🖼 الشعار: {'موجود' if s['logo'] else 'لا يوجد'}\n"
            f"🕵️ UA: {s['user_agent'] or 'افتراضي'}\n"
            f"🔗 الرابط: {BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8"
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

    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_playlist = os.path.join(out_dir, "index.m3u8")

    if mode == "copy":
        # وضع النسخ المباشر (بدون ترميز، بدون شعار)
        base_cmd = [
            "ffmpeg",
            "-re",
            "-user_agent", user_agent,
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-rw_timeout", "10000000",
            "-fflags", "+genpts+discardcorrupt",
            "-i", src,
            "-c:v", "copy",
            "-c:a", "copy",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_playlist
        ]
    else:
        # وضع الترميز (الشعار مدعوم)
        base_cmd = [
            "ffmpeg",
            "-re",
            "-user_agent", user_agent,
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-rw_timeout", "10000000",
            "-fflags", "+genpts+discardcorrupt",
            "-i", src
        ]
        if logo:
            base_cmd += [
                "-i", logo,
                "-filter_complex",
                "[1:v][0:v] scale2ref=iw:ih [logo][ref]; [ref][logo] overlay=0:0"
            ]

        base_cmd += [
            "-c:v", "libx264",
            "-preset", "superfast",
            "-crf", "23",
            "-b:v", "4000k",
            "-maxrate", "4000k",
            "-bufsize", "8000k",
            "-vsync", "cfr",
            "-r", "30",
            "-threads", "3",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_playlist
        ]

    fallback_cmd = None
    if mode == "transcode":
        # الشاشة السوداء فقط لوضع الترميز
        fallback_cmd = [
            "ffmpeg", "-re",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30",
            "-i", logo if logo else "color=c=black:s=1920x1080:r=30",
            "-filter_complex", "[0:v][1:v]overlay=0:0" if logo else "null",
            "-c:v", "libx264", "-preset", "superfast", "-crf", "23",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_playlist
        ]

    s["active"] = True
    s["start_time"] = time.time()
    save_streams()

    retries = 3
    while s["active"]:
        proc = await asyncio.create_subprocess_exec(*base_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        s["process"] = proc
        s["fallback"] = False
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
    if not s:
        return
    logger.info(f"Stopping stream {sid} - cleaning up resources")
    if s.get("process"):
        try:
            s["process"].kill()
            await s["process"].wait()
        except Exception as e:
            logger.error(f"Error killing process: {e}")
        s["process"] = None
    s["active"] = False
    s["source_online"] = False
    s["fallback"] = False
    stream_hls_dir = os.path.join(HLS_DIR, sid)
    if os.path.exists(stream_hls_dir):
        try:
            shutil.rmtree(stream_hls_dir)
            logger.info(f"Removed HLS directory: {stream_hls_dir}")
        except Exception as e:
            logger.error(f"Error removing HLS directory: {e}")
    if isinstance(s.get("viewers"), set):
        s["viewers"].clear()
    else:
        s["viewers"] = set()
    s["last_fps"] = "?"
    s["uptime"] = "00:00:00"
    s["start_time"] = 0
    save_streams()
    await update_panel_message(sid, bot)
    logger.info(f"Stream {sid} fully cleaned up")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_http_server())

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    logger.info("Rplay Dual Mode Ready")
    app.run_polling()