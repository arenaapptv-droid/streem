import asyncio
import json
import os
import time
from aiohttp import web
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ========== الإعدادات ==========
with open("settings.json") as f:
    cfg = json.load(f)
TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]

HLS_DIR = "/tmp/hls"
os.makedirs(HLS_DIR, exist_ok=True)
PORT = 8080
BASE_URL = "http://164.68.102.28"
STREAMS_FILE = "streams.json"

streams = {}
if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        streams = json.load(f)

def save():
    with open(STREAMS_FILE, "w") as f:
        json.dump(streams, f, indent=2)

# ========== أزرار الرد ==========
reply_kb = ReplyKeyboardMarkup([
    ["📺 قائمة HLS", "📡 قائمة RTMP"],
    ["➕ إضافة بث", "🖥 مراقبة"]
], resize_keyboard=True)

# ========== خادم HLS ==========
async def hls_handler(request):
    name = request.match_info["name"]
    filename = request.match_info.get("file", "index.m3u8")
    path = os.path.join(HLS_DIR, name, filename)
    if not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path)

async def start_http():
    app = web.Application()
    app.router.add_get("/live/{name}/{file:.*}", hls_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"HTTP server on port {PORT}")

# ========== أوامر البوت ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("غير مصرح")
        return
    await update.message.reply_text(
        "مرحباً في بوت البث المباشر\nاختر من الأزرار أدناه:",
        reply_markup=reply_kb
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    text = update.message.text
    print(f"Received: {text}")  # للتأكد من وصول النص

    # قائمة HLS
    if text == "📺 قائمة HLS":
        if not streams:
            await update.message.reply_text("لا توجد بثوث HLS حالياً")
            return
        msg = "📺 **بثوث HLS**:\n"
        for sid, s in streams.items():
            if s.get("type") == "hls":
                status = "🟢" if s.get("active") else "🔴"
                msg += f"{status} {s['name']}\n"
        await update.message.reply_text(msg)

    # قائمة RTMP
    elif text == "📡 قائمة RTMP":
        if not streams:
            await update.message.reply_text("لا توجد بثوث RTMP حالياً")
            return
        msg = "📡 **بثوث RTMP**:\n"
        for sid, s in streams.items():
            if s.get("type") == "rtmp":
                status = "🟢" if s.get("active") else "🔴"
                msg += f"{status} {s['name']}\n"
        await update.message.reply_text(msg)

    # إضافة بث
    elif text == "➕ إضافة بث":
        context.user_data["step"] = "wait_name"
        await update.message.reply_text("أرسل اسم البث الجديد:")

    # مراقبة
    elif text == "🖥 مراقبة":
        cpu = "غير متاح"
        try:
            with open("/proc/loadavg") as f:
                cpu = f.read().split()[0]
        except:
            pass
        await update.message.reply_text(f"🖥 حمل المعالج: {cpu}\n📡 عدد البثوث: {len(streams)}")

    # معالجة خطوات الإضافة
    elif context.user_data.get("step") == "wait_name":
        name = text.strip()
        sid = name.replace(" ", "_") + str(int(time.time()))
        streams[sid] = {
            "name": name,
            "source": "",
            "type": "hls",
            "active": False,
            "mode": "copy",
            "ua": "ExoPlayerLib/2.18.5"
        }
        save()
        context.user_data["step"] = "wait_source"
        context.user_data["sid"] = sid
        await update.message.reply_text(f"تم إضافة {name}\nالآن أرسل رابط المصدر (m3u8 أو مباشر):")

    elif context.user_data.get("step") == "wait_source":
        sid = context.user_data.get("sid")
        if sid and sid in streams:
            streams[sid]["source"] = text
            save()
            context.user_data["step"] = None
            # تشغيل البث تلقائياً
            asyncio.create_task(run_stream(sid, update.message.chat_id, context.bot))
            await update.message.reply_text(f"✅ تم حفظ المصدر وسيبدأ البث خلال لحظات\nرابط المشاهدة:\n{BASE_URL}:{PORT}/live/{sid}/index.m3u8")
        else:
            await update.message.reply_text("خطأ في البيانات، حاول مرة أخرى")

    else:
        await update.message.reply_text("اختر من الأزرار المتاحة")

# ========== تشغيل البث ==========
async def run_stream(sid, chat_id, bot):
    s = streams[sid]
    src = s["source"]
    ua = s.get("ua", "ExoPlayerLib/2.18.5")
    
    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "index.m3u8")
    
    cmd = [
        "ffmpeg", "-re",
        "-user_agent", ua,
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-i", src,
        "-c:v", "copy",
        "-c:a", "aac",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments",
        out_file
    ]
    
    s["active"] = True
    save()
    
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    s["process"] = sid  # حفظ معرف العملية
    await proc.wait()
    
    s["active"] = False
    save()
    await bot.send_message(chat_id, f"⏹ توقف البث {s['name']}")

# ========== التشغيل ==========
async def main():
    await start_http()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())