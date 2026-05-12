import asyncio, re, time, json, os, logging
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ========== الإعدادات ==========
with open("settings.json", "r") as f:
    settings = json.load(f)
    TOKEN = settings["TOKEN"]
    ADMIN_ID = settings["ADMIN_ID"]

HLS_PORT = 8080               # المنفذ الذي سيستمع عليه خادم HLS
BASE_URL = "http://164.68.102.28"  # عنوان سيرفرك (غيّره حسب عنوان VPS)

STREAMS_FILE = "streams_hls.json"
streams = {}                  # {stream_id: {"source": str, "active": bool}}

def load_streams():
    if os.path.exists(STREAMS_FILE):
        try:
            with open(STREAMS_FILE) as f:
                return json.load(f)
        except: pass
    return {f"stream_{i}": {"source": "", "active": False} for i in range(1, 10)}

def save_streams():
    with open(STREAMS_FILE, "w") as f:
        json.dump(streams, f, indent=2)

streams = load_streams()

# ========== وكيل HLS ==========
async def proxy_hls(request):
    """يعيد توجيه طلبات HLS إلى المصدر الأصلي"""
    stream_name = request.match_info.get("name")
    stream_id = f"stream_{stream_name.split('_')[1]}" if "_" in stream_name else None

    if not stream_id or not streams.get(stream_id, {}).get("active"):
        return web.Response(text="Stream not active", status=404)

    source_url = streams[stream_id]["source"]
    path = request.match_info.get("path", "")
    target_url = source_url.rsplit("/", 1)[0] + "/" + path if path else source_url

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(target_url, timeout=10) as resp:
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
    print(f"✅ HLS Proxy running on port {HLS_PORT}")

# ========== متحكم البوت ==========
async def check_admin(update: Update) -> bool:
    if update.effective_user.id != ADMIN_ID:
        if update.message:
            await update.message.reply_text("🚫 غير مصرح")
        elif update.callback_query:
            await update.callback_query.answer("🚫 غير مصرح", show_alert=True)
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
        # عرض حالة السيرفر
        cpu = 0.0  # يمكنك إضافة قراءة حقيقية
        await q.edit_message_text(f"🖥 CPU: {cpu}%", reply_markup=main_menu())
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
            url = f"{BASE_URL}:{HLS_PORT}/live/{sid}.m3u8"
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
    # تشغيل خادم HLS في الخلفية
    loop = asyncio.get_event_loop()
    loop.create_task(start_hls_server())

    # تشغيل البوت
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    print("✅ البوت يعمل...")
    app.run_polling()