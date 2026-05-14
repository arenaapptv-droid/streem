import asyncio
import json
import os
import time
import shutil

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# ================= CONFIG =================
with open("settings.json") as f:
    cfg = json.load(f)

TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]
BASE_URL = cfg.get("BASE_URL", "http://127.0.0.1")
PORT = cfg.get("PORT", 8080)

HLS_DIR = "/tmp/hls"

os.makedirs(HLS_DIR, exist_ok=True)

streams = {}
processes = {}

# ================= LIMIT =================
MAX_ENCODERS = 3
active_encoders = 0
encoder_lock = asyncio.Lock()

# ================= CPU GUARD =================
def cpu_ok():
    try:
        import psutil
        return psutil.cpu_percent() < 85
    except:
        return True

async def acquire_encoder():
    global active_encoders
    async with encoder_lock:
        if active_encoders >= MAX_ENCODERS:
            return False
        active_encoders += 1
        return True

async def release_encoder():
    global active_encoders
    async with encoder_lock:
        if active_encoders > 0:
            active_encoders -= 1

# ================= FFmpeg =================
def copy_cmd(src, out):
    return [
        "ffmpeg", "-re",
        "-fflags", "+genpts+discardcorrupt",
        "-i", src,
        "-c", "copy",
        "-f", "flv",
        "-y", out
    ]

def encode_cmd(src, out):
    return [
        "ffmpeg", "-re",
        "-i", src,

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-b:v", "4000k",
        "-maxrate", "4000k",
        "-bufsize", "8000k",
        "-g", "60",

        "-c:a", "aac",
        "-b:a", "128k",

        "-f", "flv",
        "-y", out
    ]

# ================= START STREAM =================
async def start_stream(sid):
    global active_encoders

    s = streams[sid]
    src = s["source"]
    mode = s.get("mode", "copy")

    if not cpu_ok():
        print("CPU overload")
        return

    if mode == "encode":
        ok = await acquire_encoder()
        if not ok:
            print("Max encoders reached")
            return

    out = f"rtmp://dummy/{sid}"

    cmd = copy_cmd(src, out) if mode == "copy" else encode_cmd(src, out)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )

    processes[sid] = proc
    s["active"] = True

    await proc.wait()

    processes.pop(sid, None)
    s["active"] = False

    if mode == "encode":
        await release_encoder()

    # auto restart
    await asyncio.sleep(2)
    if streams.get(sid, {}).get("auto_restart", True):
        asyncio.create_task(start_stream(sid))

# ================= STOP STREAM =================
async def stop_stream(sid):
    proc = processes.get(sid)
    if proc:
        proc.terminate()
        await asyncio.sleep(1)
        proc.kill()

    processes.pop(sid, None)

    if sid in streams:
        streams[sid]["active"] = False

# ================= TELEGRAM =================
reply_kb = ReplyKeyboardMarkup([
    ["➕ إضافة بث", "📺 قائمة"],
    ["▶️ تشغيل", "⏹ إيقاف"]
], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🚀 IPTV Bot جاهز", reply_markup=reply_kb)

# ================= HANDLE TEXT =================
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text

    if text == "➕ إضافة بث":
        context.user_data["step"] = "name"
        await update.message.reply_text("أرسل اسم البث")
        return

    if context.user_data.get("step") == "name":
        sid = text.replace(" ", "_")
        streams[sid] = {"name": text, "source": "", "mode": "copy", "active": False}
        context.user_data["sid"] = sid
        context.user_data["step"] = "source"
        await update.message.reply_text("أرسل الرابط")
        return

    if context.user_data.get("step") == "source":
        sid = context.user_data["sid"]
        streams[sid]["source"] = text
        context.user_data.clear()
        await update.message.reply_text("تم إضافة البث")
        return

# ================= CALLBACK =================
async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data

    if data.startswith("start_"):
        sid = data.split("_")[1]
        asyncio.create_task(start_stream(sid))
        await q.edit_message_text("تشغيل...")

    if data.startswith("stop_"):
        sid = data.split("_")[1]
        await stop_stream(sid)
        await q.edit_message_text("تم الإيقاف")

# ================= MAIN =================
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(CallbackQueryHandler(callback))

    print("🚀 IPTV Bot Running")
    app.run_polling()

if __name__ == "__main__":
    main()