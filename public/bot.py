import asyncio, time, json, os, logging
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
    streams[sid] = {"source": "", "logo": "", "active": False, "process": None}

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

# ========== خادم HLS ثابت ==========
async def handle_hls(request):
    name = request.match_info["name"]
    file = request.match_info.get("file", "index.m3u8")
    path = os.path.join(HLS_DIR, name, file)
    if not os.path.exists(path):
        return web.Response(status=404)
    with open(path, "rb") as f:
        body = f.read()
    return web.Response(body=body)

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
    await update.message.reply_text("🖥 **Rplay Streamer Pro**", reply_markup=main_menu())

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
        await q.edit_message_text("🖥 **Rplay Streamer Pro**", reply_markup=main_menu())

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

    # أمر FFmpeg مع تحويل H.265 إلى H.264 وإضافة شعار
    if logo:
        cmd = [
            "ffmpeg",
            "-re", "-i", src,
            "-i", logo,
            "-filter_complex",
            "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2[base]; "
            "[1:v]scale=1920:1080[logo]; [base][logo]overlay=0:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_playlist
        ]
    else:
        cmd = [
            "ffmpeg",
            "-re", "-i", src,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_playlist
        ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        s["process"] = proc
        s["active"] = True
        save_streams()
        url = f"{BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8"
        await bot.send_message(ADMIN_ID, f"✅ بدأ البث {sid}\n🔗 {url}")

        # انتظار العملية
        await proc.wait()
    except Exception as e:
        logger.error(f"Failed to start stream {sid}: {e}")
        await bot.send_message(ADMIN_ID, f"❌ فشل تشغيل {sid}: {e}")
    finally:
        s["active"] = False
        s["process"] = None
        save_streams()
        await bot.send_message(ADMIN_ID, f"⏹ توقف البث {sid}")

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
    logger.info("Rplay Streamer Pro started")
    app.run_polling()