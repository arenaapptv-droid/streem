import asyncio
import json
import os
import time
import subprocess
import shutil
from datetime import timedelta
from aiohttp import web
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# تحميل الإعدادات
with open("settings.json") as f:
    cfg = json.load(f)
TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]

# إعدادات البث
HLS_DIR = "/tmp/hls"
os.makedirs(HLS_DIR, exist_ok=True)
PORT = 8080
BASE_URL = "http://164.68.102.28"
STREAMS_FILE = "streams.json"

streams = {}
processes = {}

# تحميل البيانات
if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        streams = json.load(f)

def save():
    with open(STREAMS_FILE, "w") as f:
        json.dump(streams, f, indent=2)

# أزرار الرد
reply_kb = ReplyKeyboardMarkup([
    ["📺 HLS", "📡 RTMP"],
    ["➕ إضافة", "🖥 مراقبة"]
], resize_keyboard=True)

# خادم HLS
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

# تشغيل البث
async def run_stream(sid):
    s = streams[sid]
    src = s["source"]
    ua = s.get("ua", "ExoPlayerLib/2.18.5")
    mode = s.get("mode", "copy")
    typ = s.get("type", "hls")
    
    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "index.m3u8")
    
    base = ["ffmpeg", "-re", "-user_agent", ua, "-reconnect", "1", 
            "-reconnect_streamed", "1", "-i", src]
    
    if typ == "hls":
        if mode == "copy":
            cmd = base + ["-c:v", "copy", "-c:a", "copy", "-f", "hls", 
                          "-hls_time", "2", "-hls_list_size", "5", 
                          "-hls_flags", "delete_segments", out_file]
        else:
            cmd = base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                          "-c:a", "aac", "-b:a", "128k", "-f", "hls", 
                          "-hls_time", "2", "-hls_list_size", "5", 
                          "-hls_flags", "delete_segments", out_file]
    else:
        rtmp = f"rtmp://{s['rtmp_server']}/{s['rtmp_key']}"
        cmd = base + ["-c:v", "copy" if mode=="copy" else "libx264", 
                      "-c:a", "aac", "-f", "flv", rtmp]
    
    s["active"] = True
    s["start"] = time.time()
    save()
    
    proc = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.DEVNULL)
    processes[sid] = proc
    await proc.wait()
    
    s["active"] = False
    processes.pop(sid, None)
    save()

# أوامر البوت
async def start(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("غير مصرح")
        return
    await update.message.reply_text("مرحباً بك في بوت البث", reply_markup=reply_kb)

async def handle_message(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    text = update.message.text
    
    if text == "📺 HLS":
        msg = "قائمة بثوث HLS:\n"
        for sid, s in streams.items():
            if s["type"] == "hls":
                status = "🟢" if s.get("active") else "🔴"
                msg += f"{status} {s['name']}\n"
        if not msg.strip():
            msg = "لا توجد بثوث HLS"
        await update.message.reply_text(msg)
        
    elif text == "📡 RTMP":
        msg = "قائمة بثوث RTMP:\n"
        for sid, s in streams.items():
            if s["type"] == "rtmp":
                status = "🟢" if s.get("active") else "🔴"
                msg += f"{status} {s['name']}\n"
        if not msg.strip():
            msg = "لا توجد بثوث RTMP"
        await update.message.reply_text(msg)
        
    elif text == "➕ إضافة":
        context.user_data["step"] = "add_name"
        await update.message.reply_text("أرسل اسم البث الجديد:")
        
    elif text == "🖥 مراقبة":
        status = get_system_status()
        await update.message.reply_text(status)
        
    else:
        # معالجة خطوات الإضافة والتعديل
        step = context.user_data.get("step")
        if step == "add_name":
            name = text
            sid = name.replace(" ", "_") + str(int(time.time()))
            streams[sid] = {
                "name": name, "source": "", "ua": "", "mode": "copy",
                "type": "hls", "rtmp_server": "", "rtmp_key": "",
                "active": False
            }
            save()
            context.user_data["step"] = None
            context.user_data["adding"] = sid
            await update.message.reply_text(f"تم إضافة {name}\nالآن أرسل رابط المصدر:")
            
        elif step == "add_source":
            sid = context.user_data.get("adding")
            if sid and sid in streams:
                streams[sid]["source"] = text
                save()
                context.user_data["step"] = None
                await update.message.reply_text(f"تم حفظ المصدر.\nرابط المشاهدة:\n{BASE_URL}:{PORT}/live/{sid}/index.m3u8")
                
        elif step == "set_type":
            # سيأتي من الكول باك (نستخدم رسالة نصية للتجاوب)
            pass

def get_system_status():
    cpu = 0
    try:
        with open("/proc/loadavg") as f:
            cpu = f.read().split()[0]
    except: pass
    return f"🖥 الحمل: {cpu}\n📺 عدد البثوث: {len(streams)}"

# تشغيل البوت
async def main():
    await start_http()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())