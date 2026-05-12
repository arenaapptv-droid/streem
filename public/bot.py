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
BASE_URL = "http://164.68.102.28"

STREAMS_FILE = "streams_pro.json"
streams = {}
for i in range(1, 10):
    sid = f"stream_{i}"
    streams[sid] = {"source": "", "logo": "", "active": False, "process": None, "status_msg_id": None}

if os.path.exists(STREAMS_FILE):
    try:
        with open(STREAMS_FILE) as f:
            saved = json.load(f)
            for k, v in saved.items():
                if k in streams:
                    streams[k].update(v)
    except: pass

def save_streams():
    data = {}
    for sid, s in streams.items():
        data[sid] = {"source": s["source"], "logo": s["logo"], "active": s["active"]}
    with open(STREAMS_FILE, "w") as f:
        json.dump(data, f, indent=2)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RplayStreamer")

# ========== متابعة المشاهدين (بدون تسرب) ==========
viewers = defaultdict(set)
viewer_last_seen = defaultdict(dict)

async def track_viewer(request, stream_name):
    ip = request.remote
    now = time.time()
    viewers[stream_name].add(ip)
    viewer_last_seen[stream_name][ip] = now

def clean_viewers():
    now = time.time()
    for sid in list(viewers.keys()):
        for ip in list(viewers[sid]):
            if now - viewer_last_seen[sid].get(ip, 0) > 10:
                viewers[sid].discard(ip)
                if ip in viewer_last_seen[sid]:
                    del viewer_last_seen[sid][ip]
        if not viewers[sid]:
            viewers.pop(sid, None)
        if sid in viewer_last_seen and not viewer_last_seen[sid]:
            viewer_last_seen.pop(sid, None)

# ========== خادم HLS (بدون تحميل الملفات للذاكرة) ==========
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
    logger.info(f"HTTP server running on port {HTTP_PORT}")

# ========== حالة السيرفر ==========
def get_system_status():
    cpu = 0.0
    try:
        with open("/proc/stat", "r") as st:
            line = st.readline().split()
            if line[0] == "cpu":
                idle1 = int(line[4])
                total1 = sum(map(int, line[1:5]))
        time.sleep(0.1)
        with open("/proc/stat", "r") as st:
            line = st.readline().split()
            if line[0] == "cpu":
                idle2 = int(line[4])
                total2 = sum(map(int, line[1:5]))
        delta_total = total2 - total1
        delta_idle = idle2 - idle1
        if delta_total > 0:
            cpu = 100.0 * (1.0 - delta_idle / delta_total)
    except: pass
    ram = "N/A"
    try:
        with open("/proc/meminfo") as mem:
            lines = mem.readlines()
            total = int(lines[0].split()[1]) // 1024
            avail = int([l for l in lines if "MemAvailable" in l][0].split()[1]) // 1024
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
    kb.append([InlineKeyboardButton("📥 إعداد المصدر", callback_data=f"source_{sid}")])
    kb.append([InlineKeyboardButton("🖼 إعداد الشعار", callback_data=f"logo_{sid}")])
    kb.append([InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

async def start(update, context):
    if not await check_admin(update): return
    await update.message.reply_text("🖥 **Rplay Streamer Ultra**", reply_markup=main_menu())

async def button_handler(update, context):
    q = update.callback_query
    await q.answer()
    d = q.data
    if not await check_admin(update): return

    if d == "status":
        txt = get_system_status()
        if q.message.text != txt:
            await q.edit_message_text(txt, reply_markup=main_menu())
    elif d == "main_menu":
        await q.edit_message_text("🖥 **Rplay Streamer Ultra**", reply_markup=main_menu())

    elif "_" in d:
        act, sid = d.split("_", 1)
        if act == "menu":
            await q.edit_message_text(f"🎛 {sid}", reply_markup=control_menu(sid))
        elif act == "start":
            if not streams[sid]["source"]:
                await q.edit_message_text("❌ الرجاء تعيين المصدر أولاً")
                return
            asyncio.create_task(start_stream(sid, context.bot))
        elif act == "stop":
            await stop_stream(sid, context.bot)
        elif act == "source":
            context.user_data["mode"] = f"source_{sid}"
            await q.edit_message_text(f"📥 أرسل رابط المصدر لـ {sid}:")
        elif act == "logo":
            context.user_data["mode"] = f"logo_{sid}"
            await q.edit_message_text(f"🖼 أرسل رابط الشعار لـ {sid} (أو أرسل /skip لتخطي):")

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
            if text.lower() == "/skip":
                streams[sid]["logo"] = ""
            else:
                streams[sid]["logo"] = text
            save_streams()
            await update.message.reply_text(f"✅ تم تحديث الشعار لـ {sid}")

async def start_stream(sid, bot):
    s = streams[sid]
    src = s["source"]
    logo = s.get("logo", "")
    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_playlist = os.path.join(out_dir, "index.m3u8")

    # --- أمر FFmpeg الأمثل للأداء ---
    base = [
        "ffmpeg",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "10M",
        "-probesize", "10M",
        "-i", src,
    ]

    if logo:
        cmd = base + [
            "-i", logo,
            "-filter_complex", "[0:v][1:v]overlay=0:0",
            "-c:v", "libx264", "-preset", "superfast", "-tune", "zerolatency",
            "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
            "-maxrate", "3500k", "-bufsize", "7000k",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_playlist
        ]
    else:
        cmd = base + [
            "-c:v", "libx264", "-preset", "superfast", "-tune", "zerolatency",
            "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
            "-maxrate", "3500k", "-bufsize", "7000k",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_playlist
        ]

    last_err_lines = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        s["process"] = proc
        s["active"] = True
        start_time = time.time()
        save_streams()

        msg = await bot.send_message(ADMIN_ID, f"🟢 بدأ البث {sid}\n⏳ جاري التحميل...")
        s["status_msg_id"] = msg.message_id

        last_update = time.time()
        last_fps = "?"
        last_time_str = "00:00:00"

        async def read_stderr():
            nonlocal last_fps, last_time_str, last_err_lines
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="ignore").strip()
                last_err_lines.append(decoded)
                if len(last_err_lines) > 3:
                    last_err_lines.pop(0)
                if "fps=" in decoded:
                    fps_match = re.search(r"fps=\s*([\d.]+)", decoded)
                    if fps_match:
                        last_fps = fps_match.group(1)
                    time_match = re.search(r"time=(\d+:\d+:\d+\.\d+)", decoded)
                    if time_match:
                        last_time_str = time_match.group(1)

        reader = asyncio.create_task(read_stderr())

        while proc.returncode is None:
            now = time.time()
            if now - last_update >= 5:
                last_update = now
                clean_viewers()
                uptime_seconds = int(now - start_time)
                hours = uptime_seconds // 3600
                minutes = (uptime_seconds % 3600) // 60
                seconds = uptime_seconds % 60
                uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                viewer_count = len(viewers.get(sid, set()))

                text = (
                    f"🟢 **{sid} يعمل**\n"
                    f"📊 FPS: {last_fps}\n"
                    f"⏱ الوقت: {last_time_str}\n"
                    f"🕒 مدة البث: {uptime_str}\n"
                    f"👥 المشاهدين: {viewer_count}"
                )
                try:
                    await bot.edit_message_text(
                        chat_id=ADMIN_ID,
                        message_id=msg.message_id,
                        text=text,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{sid}")]
                        ])
                    )
                except:
                    pass
            await asyncio.sleep(0.2)

        # العملية انتهت
        retcode = await proc.wait()
        with open("ffmpeg_errors.log", "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {sid} exit {retcode}\n")
            f.write("\n".join(last_err_lines) + "\n\n")

    except Exception as e:
        logger.error(f"Failed to start stream {sid}: {e}")
        await bot.send_message(ADMIN_ID, f"❌ فشل تشغيل {sid}: {e}")
    finally:
        s["active"] = False
        s["process"] = None
        save_streams()
        if s.get("status_msg_id"):
            try:
                await bot.edit_message_text(
                    chat_id=ADMIN_ID,
                    message_id=s["status_msg_id"],
                    text=f"⏹ توقف البث {sid}"
                )
            except:
                pass
            s["status_msg_id"] = None

async def stop_stream(sid, bot):
    s = streams[sid]
    if s.get("process"):
        try:
            s["process"].kill()
            await s["process"].wait()
        except: pass
        s["active"] = False
        s["process"] = None
        save_streams()
        await bot.send_message(ADMIN_ID, f"⏹ تم إيقاف {sid}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_http_server())

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    logger.info("Rplay Streamer Ultra - جاهز للإنتاج")
    app.run_polling()