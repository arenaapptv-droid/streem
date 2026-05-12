import asyncio, time, json, os, logging, re
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
BASE_URL = "http://164.68.102.28"   # غيّره إلى عنوان VPS الصحيح

STREAMS_FILE = "streams_pro.json"
streams = {}
for i in range(1, 10):
    sid = f"stream_{i}"
    streams[sid] = {
        "source": "", "logo": "", "user_agent": "", "active": False,
        "process": None, "fallback": False, "status_msg_id": None,
        "source_online": False, "viewers": set()
    }

if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        saved = json.load(f)
        for k, v in saved.items():
            if k in streams:
                v.pop("process", None)
                v.pop("viewers", None)
                streams[k].update(v)

def save_streams():
    data = {}
    for sid, s in streams.items():
        data[sid] = {
            "source": s["source"], "logo": s["logo"], "user_agent": s["user_agent"],
            "active": s["active"], "fallback": s["fallback"], "source_online": s["source_online"]
        }
    with open(STREAMS_FILE, "w") as f:
        json.dump(data, f, indent=2)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RplayStable")

# ========== متابعة المشاهدين ==========
viewer_last_seen = defaultdict(dict)

async def track_viewer(request, stream_name):
    ip = request.remote
    now = time.time()
    streams[stream_name]["viewers"].add(ip)
    viewer_last_seen[stream_name][ip] = now

def clean_viewers():
    now = time.time()
    for sid in streams:
        s = streams[sid]
        for ip in list(s["viewers"]):
            if now - viewer_last_seen[sid].get(ip, 0) > 10:
                s["viewers"].discard(ip)
                if ip in viewer_last_seen[sid]:
                    del viewer_last_seen[sid][ip]

# ========== خادم HLS ==========
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

# ========== حالة السيرفر ==========
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

# ========== دوال البوت ==========
async def check_admin(update):
    if update.effective_user.id != ADMIN_ID:
        if update.message: await update.message.reply_text("🚫 غير مصرح")
        elif update.callback_query: await update.callback_query.answer("🚫 غير مصرح", show_alert=True)
        return False
    return True

def main_menu():
    kb = []
    for i in range(1, 10):
        kb.append([InlineKeyboardButton(f"🎬 Stream {i}", callback_data=f"menu_stream_{i}")])
    kb.append([InlineKeyboardButton("🖥 حالة السيرفر", callback_data="status")])
    return InlineKeyboardMarkup(kb)

def control_menu(sid):
    s = streams[sid]
    kb = []
    if s["active"]:
        kb.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{sid}")])
    else:
        kb.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{sid}")])
    kb.append([InlineKeyboardButton("📥 المصدر", callback_data=f"source_{sid}")])
    kb.append([InlineKeyboardButton("🖼 الشعار", callback_data=f"logo_{sid}")])
    kb.append([InlineKeyboardButton("🕵️ User-Agent", callback_data=f"ua_{sid}")])
    kb.append([InlineKeyboardButton("ℹ️ حالة البث", callback_data=f"info_{sid}")])
    kb.append([InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

async def start(update, context):
    if not await check_admin(update): return
    await update.message.reply_text("🖥 **Rplay Stable**", reply_markup=main_menu())

async def button_handler(update, context):
    q = update.callback_query
    await q.answer()
    d = q.data
    if not await check_admin(update): return
    if d == "status":
        await q.edit_message_text(get_system_status(), reply_markup=main_menu())
    elif d == "main_menu":
        await q.edit_message_text("🖥 **Rplay Stable**", reply_markup=main_menu())
    elif "_" in d:
        act, sid = d.split("_", 1)
        if act == "menu":
            await q.edit_message_text(f"🎛 {sid}", reply_markup=control_menu(sid))
        elif act == "start":
            if not streams[sid]["source"]:
                await q.edit_message_text("❌ عيّن المصدر أولاً")
                return
            asyncio.create_task(start_stream(sid, context.bot))
            await q.edit_message_text(f"⏳ جاري تشغيل {sid}...")
        elif act == "stop":
            await stop_stream(sid, context.bot)
            await q.edit_message_text(f"⏹ تم إيقاف {sid}")
        elif act == "source":
            context.user_data["mode"] = f"source_{sid}"
            await q.edit_message_text(f"📥 أرسل رابط المصدر لـ {sid}:")
        elif act == "logo":
            context.user_data["mode"] = f"logo_{sid}"
            await q.edit_message_text(f"🖼 أرسل رابط الشعار لـ {sid} (أو /skip)")
        elif act == "ua":
            context.user_data["mode"] = f"ua_{sid}"
            await q.edit_message_text(f"🕵️ أرسل User-Agent لـ {sid} (أو /skip)")
        elif act == "info":
            await update_status(sid, context.bot)

async def msg_handler(update, context):
    if not await check_admin(update): return
    text = update.message.text.strip()
    mode = context.user_data.get("mode")
    if mode and "_" in mode:
        act, sid = mode.split("_", 1)
        context.user_data["mode"] = None
        if act == "source":
            streams[sid]["source"] = text
            save_streams()
            await update.message.reply_text(f"✅ تم حفظ المصدر لـ {sid}")
        elif act == "logo":
            if text.lower() != "/skip":
                streams[sid]["logo"] = text
                save_streams()
                await update.message.reply_text("✅ تم تحديث الشعار")
            else:
                await update.message.reply_text("✅ تم تخطي الشعار")
        elif act == "ua":
            streams[sid]["user_agent"] = "" if text.lower() == "/skip" else text
            save_streams()
            await update.message.reply_text(f"✅ تم تحديث User-Agent لـ {sid}")

async def update_status(sid, bot):
    s = streams[sid]
    online = "🟢 المصدر شغال" if s["source_online"] else "🔴 المصدر طافي"
    viewers = len(s["viewers"])
    text = f"📡 **{sid}**\n{online}\n👥 المشاهدين: {viewers}"
    try:
        await bot.edit_message_text(chat_id=ADMIN_ID, message_id=s["status_msg_id"], text=text)
    except: pass

async def start_stream(sid, bot):
    s = streams[sid]
    src = s["source"]
    logo = s.get("logo", "")
    user_agent = s.get("user_agent", "ExoPlayerLib/2.18.5")   # <-- افتراضي قوي

    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_playlist = os.path.join(out_dir, "index.m3u8")

    # أمر ffmpeg الأساسي (نفسه بالضبط ولكن مع user‑agent)
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
        "-preset", "ultrafast",
        "-crf", "23",
        "-b:v", "9000k",
        "-maxrate", "9000k",
        "-bufsize", "18000k",
        "-vsync", "cfr",
        "-r", "30",
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

    # بث احتياطي (شاشة سوداء + شعار)
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
    save_streams()
    msg = await bot.send_message(ADMIN_ID, f"🟢 بدأ البث {sid}")
    s["status_msg_id"] = msg.message_id

    retries = 3  # عدد المحاولات قبل الانتقال للاحتياطي

    while s["active"]:
        proc = await asyncio.create_subprocess_exec(*base_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        s["process"] = proc
        s["fallback"] = False
        s["source_online"] = True

        last_fps = "?"
        async def read_stderr():
            nonlocal last_fps
            while True:
                line = await proc.stderr.readline()
                if not line: break
                decoded = line.decode(errors="ignore").strip()
                if "fps=" in decoded:
                    m = re.search(r"fps=\s*([\d.]+)", decoded)
                    if m: last_fps = m.group(1)

        reader = asyncio.create_task(read_stderr())
        start_time = time.time()

        while proc.returncode is None:
            clean_viewers()
            await asyncio.sleep(10)
            viewers = len(s["viewers"])
            uptime = int(time.time() - start_time)
            text = (
                f"🟢 **{sid} يعمل**\n"
                f"📊 FPS: {last_fps}\n"
                f"🕒 المدة: {uptime//3600:02}:{(uptime%3600)//60:02}:{uptime%60:02}\n"
                f"👥 المشاهدين: {viewers}"
            )
            try:
                await bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg.message_id, text=text)
            except: pass

        await proc.wait()
        s["source_online"] = False

        # حفظ الخطأ
        with open("ffmpeg_errors.log", "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {sid} exit code {proc.returncode}\n")

        retries -= 1
        if retries > 0:
            await bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg.message_id,
                                        text=f"⚠️ {sid} فشل المصدر - إعادة المحاولة {3-retries}/3...")
            await asyncio.sleep(2)
            continue  # يحاول مجددًا

        # استنفدت المحاولات ← شاشة سوداء
        if s["active"]:
            fallback_proc = await asyncio.create_subprocess_exec(*fallback_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            s["process"] = fallback_proc
            s["fallback"] = True
            await bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg.message_id, text=f"🖤 {sid} شاشة سوداء")
            # يبقى على الشاشة السوداء لمدة 20 ثانية ثم يكسر الحلقة ليبدأ من جديد
            await asyncio.sleep(20)
            fallback_proc.kill()
            await fallback_proc.wait()
            retries = 3   # إعادة تعيين العداد للمحاولات القادمة
        else:
            break

    if s.get("process"):
        s["process"].kill()
    s["active"] = False
    save_streams()
    await bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg.message_id, text=f"⏹ توقف {sid}")

async def stop_stream(sid, bot):
    s = streams[sid]
    s["active"] = False
    if s.get("process"):
        try:
            s["process"].kill()
        except: pass
    await bot.send_message(ADMIN_ID, f"⏹ تم إيقاف {sid}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_http_server())

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    logger.info("Rplay Stable – ready with auto user‑agent")
    app.run_polling()