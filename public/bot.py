import asyncio, time, json, os, logging, re, subprocess
from collections import defaultdict
from aiohttp import web, ClientSession
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
BASE_URL = "http://164.68.102.28"  # غيّره إلى عنوان VPS الصحيح

STREAMS_FILE = "streams_pro.json"
streams = {}
for i in range(1, 10):
    sid = f"stream_{i}"
    streams[sid] = {
        "source": "", "logo": "", "user_agent": "", "active": False, "fallback_ready": False
    }

if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        saved = json.load(f)
        for k, v in saved.items():
            if k in streams:
                streams[k].update(v)

def save_streams():
    data = {}
    for sid, s in streams.items():
        data[sid] = {
            "source": s["source"],
            "logo": s["logo"],
            "user_agent": s.get("user_agent", ""),
            "active": s["active"],
            "fallback_ready": s["fallback_ready"]
        }
    with open(STREAMS_FILE, "w") as f:
        json.dump(data, f, indent=2)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StableProxy")

# ========== توليد فيديو احتياطي (مرة واحدة) ==========
def generate_fallback(sid):
    s = streams[sid]
    logo = s.get("logo", "")
    out_dir = os.path.join(HLS_DIR, sid, "fallback")
    os.makedirs(out_dir, exist_ok=True)
    out_playlist = os.path.join(out_dir, "index.m3u8")

    if logo:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30",
            "-i", logo,
            "-filter_complex", "[1:v]scale=iw:ih[logo];[0:v][logo]overlay=0:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-t", "10",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_playlist
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-t", "10",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_playlist
        ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        s["fallback_ready"] = True
        save_streams()
        return True
    except Exception as e:
        logger.error(f"Fallback generation failed for {sid}: {e}")
        return False

# ========== بروكسي HLS الذكي (مع User-Agent) ==========
async def proxy_handler(request):
    name = request.match_info["name"]
    file = request.match_info.get("file", "index.m3u8")
    sid = name
    s = streams.get(sid)

    if not s or not s.get("active"):
        return web.Response(status=404)

    source_url = s["source"]
    if file == "index.m3u8":
        target = source_url
    else:
        base = source_url.rsplit("/", 1)[0]
        target = f"{base}/{file}"

    # إعداد الهيدرات
    headers = {}
    user_agent = s.get("user_agent", "")
    if user_agent:
        headers["User-Agent"] = user_agent

    try:
        async with ClientSession() as session:
            async with session.get(target, timeout=2, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return web.Response(body=data, content_type=resp.content_type)
    except:
        pass

    # العودة للملف الاحتياطي
    fallback_path = os.path.join(HLS_DIR, sid, "fallback", file)
    if os.path.exists(fallback_path):
        return web.FileResponse(fallback_path)
    else:
        return web.Response(status=404)

async def start_http_server():
    app = web.Application()
    app.router.add_get("/live/{name}/{file:.*}", proxy_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    logger.info(f"Proxy server running on port {HTTP_PORT}")

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
    kb.append([InlineKeyboardButton("🕵️ User-Agent", callback_data=f"ua_{sid}")])
    kb.append([InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

async def start(update, context):
    if not await check_admin(update): return
    await update.message.reply_text("🖥 **Rplay Ultra Stable**", reply_markup=main_menu())

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
        await q.edit_message_text("🖥 **Rplay Ultra Stable**", reply_markup=main_menu())

    elif "_" in d:
        act, sid = d.split("_", 1)
        if act == "menu":
            await q.edit_message_text(f"🎛 {sid}", reply_markup=control_menu(sid))
        elif act == "start":
            if not streams[sid]["source"]:
                await q.edit_message_text("❌ الرجاء تعيين المصدر أولاً")
                return
            streams[sid]["active"] = True
            save_streams()
            await q.edit_message_text(f"✅ بدأ البث {sid}\n🔗 {BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8")
        elif act == "stop":
            streams[sid]["active"] = False
            save_streams()
            await q.edit_message_text(f"⏹ تم إيقاف {sid}")
        elif act == "source":
            context.user_data["mode"] = f"source_{sid}"
            await q.edit_message_text(f"📥 أرسل رابط المصدر لـ {sid}:")
        elif act == "logo":
            context.user_data["mode"] = f"logo_{sid}"
            await q.edit_message_text(f"🖼 أرسل رابط الشعار لـ {sid} (أو /skip)")
        elif act == "ua":
            context.user_data["mode"] = f"ua_{sid}"
            await q.edit_message_text(f"🕵️ أرسل User-Agent لـ {sid} (أو /skip للحذف):")

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
                await update.message.reply_text("⏳ جاري تجهيز الشعار الاحتياطي...")
                if generate_fallback(sid):
                    await update.message.reply_text(f"✅ الشعار الاحتياطي جاهز لـ {sid}")
                else:
                    await update.message.reply_text(f"❌ فشل تجهيز الشعار")
            else:
                await update.message.reply_text("✅ تم تخطي الشعار")
        elif act == "ua":
            if text.lower() == "/skip":
                streams[sid]["user_agent"] = ""
            else:
                streams[sid]["user_agent"] = text
            save_streams()
            await update.message.reply_text(f"✅ تم تحديث User-Agent لـ {sid}")

if __name__ == "__main__":
    for sid in streams:
        if streams[sid].get("logo") and not streams[sid].get("fallback_ready"):
            generate_fallback(sid)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_http_server())

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    logger.info("Ultra Stable proxy with UA ready")
    app.run_polling()