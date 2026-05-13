import asyncio, time, json, os, re, shutil, logging
from collections import defaultdict
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ========== الإعدادات ==========
with open("settings.json") as f:
    cfg = json.load(f)
    TOKEN = cfg["TOKEN"]
    ADMIN_ID = cfg["ADMIN_ID"]
    VIDEO_BITRATE = cfg.get("VIDEO_BITRATE", "6000k")
    MAXRATE = cfg.get("MAXRATE", "9000k")
    BUFSIZE = cfg.get("BUFSIZE", "18000k")
    AUDIO_BITRATE = cfg.get("AUDIO_BITRATE", "128k")
    PRESET = cfg.get("PRESET", "faster")
    FRAMERATE = cfg.get("FRAMERATE", 30)

HLS_DIR = "/tmp/hls"
os.makedirs(HLS_DIR, exist_ok=True)
PORT = 8080
BASE_URL = "http://164.68.102.28"
STREAMS_FILE = "streams.json"

streams = {}
viewers = defaultdict(set)
viewer_last = defaultdict(dict)

def load():
    global streams
    if os.path.exists(STREAMS_FILE):
        with open(STREAMS_FILE) as f:
            streams = json.load(f)
        for sid in streams:
            streams[sid]["process"] = None
            streams[sid].setdefault("mode", "transcode")
            streams[sid].setdefault("type", "hls")
            streams[sid].setdefault("rtmp_server", "")
            streams[sid].setdefault("rtmp_key", "")

def save():
    d = {}
    for sid, v in streams.items():
        vc = v.copy()
        vc.pop("process", None)
        d[sid] = vc
    with open(STREAMS_FILE, "w") as f:
        json.dump(d, f, indent=2)

load()

# ========== خادم HLS ==========
async def hls_handler(request):
    name = request.match_info["name"]
    file = request.match_info.get("file", "index.m3u8")
    path = os.path.join(HLS_DIR, name, file)
    if not os.path.exists(path):
        return web.Response(status=404)
    ip = request.remote
    if name in streams and streams[name].get("type") == "hls" and streams[name].get("active"):
        viewers[name].add(ip)
        viewer_last[name][ip] = time.time()
    return web.FileResponse(path, headers={"Content-Type": "application/vnd.apple.mpegurl" if file.endswith(".m3u8") else "video/mp2t"})

async def start_server():
    app = web.Application()
    app.router.add_get("/live/{name}/{file:.*}", hls_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

# ========== واجهة البوت ==========
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 HLS", callback_data="list_hls")],
        [InlineKeyboardButton("📡 RTMP", callback_data="list_rtmp")],
        [InlineKeyboardButton("➕ إضافة بث", callback_data="add")],
        [InlineKeyboardButton("🖥 مراقبة", callback_data="monitor")]
    ])

def stream_list(typ):
    kb = []
    for sid, v in streams.items():
        if v["type"] != typ: continue
        status = "🟢" if v.get("active") else "⏹"
        kb.append([InlineKeyboardButton(f"{status} {v['name']}", callback_data=f"panel_{sid}")])
    kb.append([InlineKeyboardButton("🔙 القائمة", callback_data="menu")])
    return InlineKeyboardMarkup(kb)

def panel_kb(sid, s):
    active = s.get("active", False)
    mode = s.get("mode", "transcode")
    kb = []
    kb.append([InlineKeyboardButton("⏹ إيقاف" if active else "▶️ تشغيل", callback_data=f"stop_{sid}" if active else f"start_{sid}")])
    kb.append([InlineKeyboardButton("📥 مصدر", callback_data=f"src_{sid}"), InlineKeyboardButton("🖼 شعار", callback_data=f"logo_{sid}")])
    kb.append([InlineKeyboardButton("🕵️ UA", callback_data=f"ua_{sid}"), InlineKeyboardButton("✏️ إعادة تسمية", callback_data=f"rename_{sid}")])
    if s["type"] == "rtmp":
        kb.append([InlineKeyboardButton("📡 خادم RTMP", callback_data=f"rtmp_srv_{sid}"), InlineKeyboardButton("🔑 مفتاح RTMP", callback_data=f"rtmp_key_{sid}")])
    kb.append([InlineKeyboardButton("🔄 نسخ مباشر" if mode=="transcode" else "⚙️ ترميز (جودة عالية)", callback_data=f"mode_{sid}")])
    kb.append([InlineKeyboardButton("🗑 حذف", callback_data=f"del_{sid}")])
    if active:
        viewer_count = len(viewers.get(sid, set())) if s["type"]=="hls" else 0
        uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s.get("start_time", 0)))
        kb.append([InlineKeyboardButton(f"🔴 FPS:{s.get('fps','?')} | ⏱{uptime} | 👥{viewer_count}", callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 القائمة", callback_data="menu")])
    return InlineKeyboardMarkup(kb)

async def update_panel(sid, bot, chat_id, msg_id):
    s = streams.get(sid)
    if not s: return
    name = s["name"]
    typ = s["type"]
    text = f"🎛️ {name} ({typ.upper()})\n📥 {s['source'] or 'لا يوجد'}\n🖼 {'✅' if s.get('logo') else '❌'}\n🕵️ {s.get('user_agent','افتراضي')}"
    if typ == "rtmp":
        text += f"\n📡 {s.get('rtmp_server','')}/{s.get('rtmp_key','')}"
    else:
        text += f"\n🔗 {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    try:
        await bot.edit_message_text(text, chat_id, msg_id, reply_markup=panel_kb(sid, s))
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Panel update error: {e}")

# ========== تشغيل البث بجودة عالية ==========
async def run_ffmpeg(sid, bot, chat_id, msg_id):
    s = streams[sid]
    src = s["source"]
    logo = s.get("logo", "")
    ua = s.get("user_agent", "ExoPlayerLib/2.18.5")
    mode = s.get("mode", "transcode")
    typ = s["type"]

    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "index.m3u8")

    # معاملات إعادة الاتصال وتحمل الأخطاء
    base = [
        "ffmpeg", "-re", "-threads", "2",
        "-user_agent", ua,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
        "-timeout", "10000000", "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "1000000", "-probesize", "1000000",
        "-i", src
    ]

    if typ == "rtmp":
        dst = f"{s['rtmp_server']}/{s['rtmp_key']}"
        if mode == "copy":
            cmd = base + ["-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-f", "flv", dst]
        else:
            filt = []
            if logo:
                filt = ["-i", logo, "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0"]
            cmd = base + filt + [
                "-c:v", "libx264", "-preset", PRESET, "-crf", "23",
                "-b:v", VIDEO_BITRATE, "-maxrate", MAXRATE, "-bufsize", BUFSIZE,
                "-g", str(FRAMERATE * 3),  # keyframe كل 3 ثوان
                "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2",
                "-f", "flv", dst
            ]
    else:  # HLS
        if mode == "copy":
            cmd = base + [
                "-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", out_file
            ]
        else:
            filt = []
            if logo:
                filt = ["-i", logo, "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0"]
            cmd = base + filt + [
                "-c:v", "libx264", "-preset", PRESET, "-crf", "23",
                "-b:v", VIDEO_BITRATE, "-maxrate", MAXRATE, "-bufsize", BUFSIZE,
                "-g", str(FRAMERATE * 3),
                "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", out_file
            ]

    s["active"] = True
    s["start_time"] = time.time()
    save()

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    s["process"] = proc

    async def read_err():
        try:
            while True:
                line = await proc.stderr.readline()
                if not line: break
                decoded = line.decode(errors="ignore")
                # تسجيل معلومات fps
                if "fps=" in decoded:
                    m = re.search(r"fps=\s*([\d.]+)", decoded)
                    if m: s["fps"] = m.group(1)
                # تسجيل أي خطأ في الترميز
                if "error" in decoded.lower():
                    logger.warning(f"FFmpeg {sid}: {decoded.strip()}")
        except Exception as e:
            logger.error(f"Stderr reader error: {e}")

    asyncio.create_task(read_err())

    # حلقة المراقبة
    while proc.returncode is None:
        # تنظيف المشاهدين
        now = time.time()
        if typ == "hls":
            for ip in list(viewers.get(sid, set())):
                if now - viewer_last[sid].get(ip, 0) > 10:
                    viewers[sid].discard(ip)
        # تحديث اللوحة
        await update_panel(sid, bot, chat_id, msg_id)
        await asyncio.sleep(5)

    # انتهت العملية
    s["active"] = False
    s["process"] = None
    save()
    await update_panel(sid, bot, chat_id, msg_id)
    logger.warning(f"Stream {sid} ended. Return code: {proc.returncode}")

# ========== أوامر البوت ==========
async def start(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return
    await update.message.reply_text("مرحبًا بك في بوت البث المباشر (جودة عالية)", reply_markup=main_menu())

async def button(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await q.answer("🚫 غير مصرح", show_alert=True)
        return
    chat_id = q.message.chat_id
    msg_id = q.message.message_id

    if data == "menu":
        await q.edit_message_text("القائمة الرئيسية", reply_markup=main_menu())
    elif data == "list_hls":
        await q.edit_message_text("قائمة بثوث HLS", reply_markup=stream_list("hls"))
    elif data == "list_rtmp":
        await q.edit_message_text("قائمة بثوث RTMP", reply_markup=stream_list("rtmp"))
    elif data == "add":
        context.user_data["step"] = "add_name"
        await q.edit_message_text("أرسل اسم البث الجديد:")
    elif data == "monitor":
        async def monitor():
            while True:
                try:
                    with open("/proc/loadavg") as f: load = f.read().split()[0]
                    with open("/proc/meminfo") as f: mem = [int(x.split()[1]) for x in f if "MemAvailable" in x][0] // 1024
                    total_mem = 0
                    with open("/proc/meminfo") as f:
                        for line in f:
                            if "MemTotal" in line:
                                total_mem = int(line.split()[1]) // 1024
                                break
                    status = f"🖥 Load: {load} | RAM: {total_mem-mem}/{total_mem} MB"
                    await q.edit_message_text(status, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="menu")]]))
                except:
                    pass
                await asyncio.sleep(5)
        asyncio.create_task(monitor())
    elif data.startswith("panel_"):
        sid = data[6:]
        s = streams.get(sid)
        if s:
            await update_panel(sid, context.bot, chat_id, msg_id)
    elif data.startswith("start_"):
        sid = data[6:]
        s = streams.get(sid)
        if not s or not s["source"]:
            await q.answer("لا يوجد مصدر!", show_alert=True)
            return
        if s["type"] == "rtmp" and (not s.get("rtmp_server") or not s.get("rtmp_key")):
            await q.answer("اضبط خادم RTMP أولاً", show_alert=True)
            return
        asyncio.create_task(run_ffmpeg(sid, context.bot, chat_id, msg_id))
        await q.answer("جاري التشغيل بجودة عالية...")
    elif data.startswith("stop_"):
        sid = data[5:]
        if streams.get(sid) and streams[sid].get("process"):
            try:
                streams[sid]["process"].kill()
            except:
                pass
            streams[sid]["active"] = False
            streams[sid]["process"] = None
            save()
        await q.answer("تم الإيقاف")
        await update_panel(sid, context.bot, chat_id, msg_id)
    elif data.startswith("src_"):
        sid = data[4:]
        context.user_data["edit"] = ("source", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل رابط المصدر:")
    elif data.startswith("logo_"):
        sid = data[5:]
        context.user_data["edit"] = ("logo", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل رابط الشعار (أو /skip):")
    elif data.startswith("ua_"):
        sid = data[3:]
        context.user_data["edit"] = ("ua", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل User-Agent (أو /skip):")
    elif data.startswith("rename_"):
        sid = data[7:]
        context.user_data["edit"] = ("name", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل الاسم الجديد:")
    elif data.startswith("rtmp_srv_"):
        sid = data[9:]
        context.user_data["edit"] = ("rtmp_server", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل رابط خادم RTMP (مثال: rtmp://server/live):")
    elif data.startswith("rtmp_key_"):
        sid = data[9:]
        context.user_data["edit"] = ("rtmp_key", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل مفتاح البث:")
    elif data.startswith("mode_"):
        sid = data[5:]
        s = streams.get(sid)
        if s:
            s["mode"] = "copy" if s.get("mode") == "transcode" else "transcode"
            save()
            if s.get("active"):
                if s.get("process"):
                    try: s["process"].kill()
                    except: pass
                asyncio.create_task(run_ffmpeg(sid, context.bot, chat_id, msg_id))
            await update_panel(sid, context.bot, chat_id, msg_id)
    elif data.startswith("del_"):
        sid = data[4:]
        if streams.get(sid) and streams[sid].get("process"):
            try: streams[sid]["process"].kill()
            except: pass
        del streams[sid]
        save()
        await q.edit_message_text("تم الحذف", reply_markup=main_menu())

async def handle_msg(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return
    text = update.message.text.strip()
    if context.user_data.get("step") == "add_name":
        name = text
        sid = name.replace(" ", "_")
        if sid in streams:
            sid = f"{sid}_{int(time.time())}"
        streams[sid] = {"name": name, "source": "", "logo": "", "user_agent": "", "type": "hls", "mode": "transcode", "rtmp_server": "", "rtmp_key": "", "active": False, "process": None}
        save()
        context.user_data["step"] = None
        await update.message.reply_text(f"تم إضافة {name}. حدد النوع:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("HLS", callback_data=f"settype_{sid}_hls"), InlineKeyboardButton("RTMP", callback_data=f"settype_{sid}_rtmp")]
        ]))
    elif context.user_data.get("edit"):
        typ, sid, chat_id, msg_id = context.user_data["edit"]
        s = streams.get(sid)
        if not s:
            await update.message.reply_text("خطأ: البث غير موجود")
            context.user_data["edit"] = None
            return
        if typ == "source":
            s["source"] = text
        elif typ == "logo":
            if text != "/skip":
                s["logo"] = text
        elif typ == "ua":
            if text != "/skip":
                s["user_agent"] = text
        elif typ == "name":
            s["name"] = text
        elif typ == "rtmp_server":
            s["rtmp_server"] = text
        elif typ == "rtmp_key":
            s["rtmp_key"] = text
        save()
        context.user_data["edit"] = None
        await update.message.reply_text("تم الحفظ")
        await update_panel(sid, context.bot, chat_id, msg_id)

async def settype(update, context):
    q = update.callback_query
    await q.answer()
    _, sid, typ = q.data.split("_")
    if sid in streams:
        streams[sid]["type"] = typ
        save()
    await q.edit_message_text(f"تم تعيين نوع {typ.upper()} للبث {streams[sid]['name']}", reply_markup=main_menu())

# ========== التشغيل ==========
async def main():
    await start_server()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button, pattern="^(?!settype_).*"))
    app.add_handler(CallbackQueryHandler(settype, pattern="^settype_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())