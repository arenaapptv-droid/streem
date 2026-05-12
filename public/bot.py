import asyncio, time, json, os
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ========== الإعدادات ==========
with open("settings.json", "r") as f:
    settings = json.load(f)
    TOKEN = settings["TOKEN"]
    ADMIN_ID = settings["ADMIN_ID"]

HLS_PORT = 8080
BASE_URL = "http://164.68.102.28"  # غيّره إلى عنوان VPS الحقيقي

STREAMS_FILE = "streams_hls.json"
streams = {f"stream_{i}": {"source": "", "active": False} for i in range(1, 10)}
if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        streams = json.load(f)

def save_streams():
    with open(STREAMS_FILE, "w") as f:
        json.dump(streams, f, indent=2)

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
    except:
        pass

    try:
        with open("/proc/meminfo", "r") as mem:
            lines = mem.readlines()
            total = int(lines[0].split()[1]) // 1024
            avail = int([l for l in lines if "MemAvailable" in l][0].split()[1]) // 1024
            used = total - avail
            ram = f"{used} / {total} MiB"
    except:
        ram = "N/A"

    return f"🖥 CPU: {cpu:.1f}% | RAM: {ram}"

# ========== وكيل HLS ==========
async def proxy_hls(request):
    name = request.match_info.get("name")
    stream_id = None
    for sid, s in streams.items():
        if s.get("active") and f"stream_{name.split('_')[-1]}" == sid:
            stream_id = sid
            break
    if not stream_id:
        return web.Response(status=404)

    source_url = streams[stream_id]["source"]
    path = request.match_info.get("path", "")
    target = source_url.rsplit("/", 1)[0] + "/" + path if path else source_url

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(target, timeout=10) as resp:
                body = await resp.read()
                return web.Response(body=body, content_type=resp.content_type)
    except:
        return web.Response(status=502)

async def start_hls_server():
    app = web.Application()
    app.router.add_get("/live/{name}.m3u8", proxy_hls)
    app.router.add_get("/live/{name}/{path:.*}", proxy_hls)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HLS_PORT)
    await site.start()
    print(f"✅ HLS Proxy on port {HLS_PORT}")

# ========== دوال تيليجرام ==========
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
    s = streams.get(sid, {})
    kb = []
    if s.get("active"):
        kb.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{sid}")])
    else:
        kb.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{sid}")])
    kb.append([InlineKeyboardButton("🔄 تغيير المصدر", callback_data=f"change_{sid}")])
    kb.append([InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

async def start(update, context):
    if not await check_admin(update): return
    await update.message.reply_text("🖥 **Rplay HLS Proxy**", reply_markup=main_menu())

async def button_handler(update, context):
    q = update.callback_query
    await q.answer()
    d = q.data
    if not await check_admin(update): return

    if d == "status":
        status_text = get_system_status()
        current_text = q.message.text
        current_markup = q.message.reply_markup
        # تجنب التعديل إذا كانت الرسالة نفسها
        if current_text == status_text and current_markup == main_menu():
            return
        await q.edit_message_text(status_text, reply_markup=main_menu())
    elif d == "main_menu":
        await q.edit_message_text("🖥 **Rplay HLS Proxy**", reply_markup=main_menu())

    elif "_" in d:
        act, sid = d.split("_", 1)
        if act == "menu":
            await q.edit_message_text(f"🎛 {sid}", reply_markup=control_menu(sid))
        elif act == "start":
            if not streams[sid].get("source"):
                await q.edit_message_text("❌ عيّن مصدرًا أولاً (تغيير المصدر)")
                return
            streams[sid]["active"] = True
            save_streams()
            url = f"{BASE_URL}:{HLS_PORT}/live/{sid.replace('_', '')}.m3u8"
            await q.edit_message_text(f"✅ بدأ البث\n🔗 {url}", reply_markup=control_menu(sid))
        elif act == "stop":
            streams[sid]["active"] = False
            save_streams()
            await q.edit_message_text(f"⏹ تم إيقاف {sid}", reply_markup=control_menu(sid))
        elif act == "change":
            context.user_data["mode"] = f"source_{sid}"
            await q.edit_message_text(f"📥 أرسل رابط m3u8 لـ {sid}:")
        elif act == "delete":
            streams[sid] = {"source": "", "active": False}
            save_streams()
            await q.edit_message_text(f"🗑 مسحت إعدادات {sid}")

async def msg_handler(update, context):
    if not await check_admin(update): return
    text = update.message.text.strip()
    mode = context.user_data.get("mode")
    if mode and mode.startswith("source_"):
        sid = mode.split("_", 1)[1]
        context.user_data["mode"] = None
        streams[sid]["source"] = text
        save_streams()
        await update.message.reply_text(f"✅ تم حفظ المصدر لـ {sid}. يمكنك تشغيله الآن.")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_hls_server())

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    print("✅ البوت يعمل...")
    app.run_polling()