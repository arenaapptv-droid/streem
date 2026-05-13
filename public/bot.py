import asyncio
import json
import os
import re
import shutil
import time
from collections import defaultdict

from aiohttp import web
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# =========================================
# PSUTIL (optional)
# =========================================
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# =========================================
# CONFIG
# =========================================
with open("settings.json") as f:
    cfg = json.load(f)

TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]

BASE_URL = "http://164.68.102.28"
PORT = 8080

HLS_DIR = "/tmp/hls"
STREAMS_FILE = "streams.json"

os.makedirs(HLS_DIR, exist_ok=True)

streams = {}
processes = {}
viewers = defaultdict(set)
viewer_last = defaultdict(dict)

if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        streams = json.load(f)
    # convert viewers from list to set if needed
    for sid, s in streams.items():
        if "viewers" in s and isinstance(s["viewers"], list):
            s["viewers"] = set(s["viewers"])
        s.setdefault("fps", "?")
        s.setdefault("logo", "")
        s.setdefault("ua", "ExoPlayerLib/2.18.5")
        s.setdefault("rtmp_server", "")
        s.setdefault("rtmp_key", "")

# =========================================
# SAVE STREAMS
# =========================================
def save_streams():
    data = {}
    for sid, s in streams.items():
        tmp = s.copy()
        if "viewers" in tmp and isinstance(tmp["viewers"], set):
            tmp["viewers"] = list(tmp["viewers"])
        tmp.pop("process", None)
        data[sid] = tmp
    with open(STREAMS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# =========================================
# KEYBOARD (main)
# =========================================
main_kb = ReplyKeyboardMarkup([
    ["📺 قائمة HLS", "📡 قائمة RTMP"],
    ["➕ إضافة بث", "🖥 مراقبة السيرفر"],
    ["🧹 تنظيف الملفات"]
], resize_keyboard=True)

# =========================================
# HTTP SERVER
# =========================================
async def track_viewer(request, sid):
    ip = request.headers.get("X-Forwarded-For", request.remote)
    if ip:
        viewers[sid].add(ip)
        viewer_last[sid][ip] = time.time()

async def hls_handler(request):
    sid = request.match_info["name"]
    filename = request.match_info.get("file", "index.m3u8")
    path = os.path.join(HLS_DIR, sid, filename)
    if not os.path.exists(path):
        return web.Response(status=404)
    await track_viewer(request, sid)
    return web.FileResponse(path)

async def start_http():
    app = web.Application()
    app.router.add_get("/live/{name}/{file:.*}", hls_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"✅ HTTP server running on port {PORT}")

# =========================================
# VIEWERS CLEANUP
# =========================================
def clean_viewers():
    now = time.time()
    for sid in list(viewers.keys()):
        for ip in list(viewers[sid]):
            if now - viewer_last[sid].get(ip, 0) > 15:
                viewers[sid].discard(ip)
                viewer_last[sid].pop(ip, None)

async def clean_viewers_loop():
    while True:
        clean_viewers()
        await asyncio.sleep(10)

# =========================================
# SYSTEM STATUS
# =========================================
def system_status():
    if PSUTIL_AVAILABLE:
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return (
            f"🖥️ **حالة السيرفر**\n"
            f"💻 CPU: {cpu}%\n"
            f"🧠 RAM: {mem.percent}% ({mem.used//(1024**2)}/{mem.total//(1024**2)} MB)\n"
            f"💾 Disk: {disk.percent}% ({disk.used//(1024**3)}/{disk.total//(1024**3)} GB)\n"
            f"📺 البثوث: {len(streams)}"
        )
    else:
        load = "N/A"
        try:
            with open("/proc/loadavg") as f:
                load = f.read().split()[0]
        except:
            pass
        return f"🖥️ الحمل: {load}\n📺 البثوث: {len(streams)}"

# =========================================
# STREAM PANEL (with full controls)
# =========================================
async def show_panel(update, sid, context):
    s = streams.get(sid)
    if not s:
        await update.message.reply_text("❌ البث غير موجود")
        return

    uptime = "00:00:00"
    if s.get("start_time"):
        uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))

    viewers_count = len(viewers.get(sid, set()))
    text = (
        f"🎛️ **{s['name']}**\n"
        f"📥 المصدر: `{s['source']}`\n"
        f"🖼 الشعار: {'✅' if s.get('logo') else '❌'}\n"
        f"🕵️ UA: `{s.get('ua')}`\n"
        f"⚙️ الوضع: {'نسخ مباشر' if s['mode']=='copy' else 'ترميز (جودة عالية)'}\n"
        f"🟢 الحالة: {'يعمل' if s.get('active') else 'متوقف'}\n"
        f"🎬 FPS: {s.get('fps','?')}\n"
        f"👥 المشاهدين: {viewers_count}\n"
        f"⏱️ التشغيل: {uptime}\n\n"
        f"🔗 {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    )

    # أزرار التحكم الكاملة
    panel_kb = ReplyKeyboardMarkup([
        ["▶️ تشغيل", "⏹ إيقاف"],
        ["📥 تغيير المصدر", "🖼 تغيير الشعار"],
        ["🕵️ تغيير UA", "✏️ إعادة تسمية"],
        ["🔄 تبديل الوضع", "🗑 حذف البث"],
        ["🔙 القائمة الرئيسية"]
    ], resize_keyboard=True)

    context.user_data["current_sid"] = sid
    await update.message.reply_text(text, reply_markup=panel_kb)

# =========================================
# FFMPEG STREAM START (with reconnection, loop, scaling)
# =========================================
async def start_stream(sid, chat_id, bot):
    s = streams[sid]
    src = s["source"]
    mode = s.get("mode", "copy")
    ua = s.get("ua", "ExoPlayerLib/2.18.5")
    logo = s.get("logo", "")

    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "index.m3u8")

    base_cmd = [
        "ffmpeg", "-re",
        "-user_agent", ua,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
        "-timeout", "10000000", "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "1000000", "-probesize", "5000000",
        "-stream_loop", "-1",
        "-i", src
    ]

    if mode == "copy":
        video_opts = ["-c:v", "copy"]
    else:
        video_opts = [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
            "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
            "-g", "90"
        ]

    audio_opts = ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]

    if logo and mode != "copy":
        cmd = base_cmd + ["-i", logo, "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0"] + video_opts + audio_opts + [
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments", out_file
        ]
    else:
        cmd = base_cmd + video_opts + audio_opts + [
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments", out_file
        ]

    # kill previous process if exists
    if sid in processes:
        try:
            processes[sid].terminate()
            await asyncio.sleep(0.5)
            processes[sid].kill()
        except:
            pass
        processes.pop(sid, None)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )
    processes[sid] = proc
    s["active"] = True
    s["start_time"] = time.time()
    save_streams()

    async def stderr_reader():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            txt = line.decode(errors="ignore").strip()
            m = re.search(r"fps=\s*([\d.]+)", txt)
            if m:
                s["fps"] = m.group(1)
            if "error" in txt.lower():
                print(f"[{sid}] ffmpeg error: {txt}")

    asyncio.create_task(stderr_reader())
    await proc.wait()

    s["active"] = False
    processes.pop(sid, None)
    save_streams()
    await bot.send_message(chat_id, f"⛔ توقف بث {s['name']}")

# =========================================
# STOP STREAM
# =========================================
async def stop_stream(sid):
    if sid in processes:
        try:
            processes[sid].terminate()
            await asyncio.sleep(0.5)
            processes[sid].kill()
        except:
            pass
        processes.pop(sid, None)
    if sid in streams:
        streams[sid]["active"] = False
        save_streams()
    # clean HLS files
    path = os.path.join(HLS_DIR, sid)
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)

# =========================================
# MESSAGE HANDLER (full admin panel)
# =========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return

    text = update.message.text
    chat_id = update.effective_chat.id

    # ----- قوائم البثوث -----
    if text == "📺 قائمة HLS":
        msg = "📺 **بثوث HLS**\n"
        for sid, s in streams.items():
            if s.get("type", "hls") == "hls":
                status = "🟢" if s.get("active") else "🔴"
                msg += f"{status} {s['name']}\n"
        if msg.strip() == "📺 **بثوث HLS**":
            msg = "لا توجد بثوث HLS"
        await update.message.reply_text(msg)

    elif text == "📡 قائمة RTMP":
        msg = "📡 **بثوث RTMP**\n"
        for sid, s in streams.items():
            if s.get("type") == "rtmp":
                status = "🟢" if s.get("active") else "🔴"
                msg += f"{status} {s['name']}\n"
        if msg.strip() == "📡 **بثوث RTMP**":
            msg = "لا توجد بثوث RTMP"
        await update.message.reply_text(msg)

    elif text == "➕ إضافة بث":
        context.user_data["step"] = "add_name"
        await update.message.reply_text("📝 أرسل اسم البث الجديد:")

    elif text == "🖥 مراقبة السيرفر":
        await update.message.reply_text(system_status())

    elif text == "🧹 تنظيف الملفات":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        await update.message.reply_text("✅ تم تنظيف مجلد HLS")

    elif context.user_data.get("step") == "add_name":
        name = text.strip()
        sid = name.replace(" ", "_") + str(int(time.time()))
        streams[sid] = {
            "name": name,
            "source": "",
            "type": "hls",
            "mode": "copy",
            "active": False,
            "fps": "?",
            "ua": "ExoPlayerLib/2.18.5",
            "logo": "",
            "rtmp_server": "",
            "rtmp_key": ""
        }
        save_streams()
        context.user_data["step"] = "add_source"
        context.user_data["sid"] = sid
        await update.message.reply_text("📥 أرسل رابط المصدر (m3u8 أو مباشر):")

    elif context.user_data.get("step") == "add_source":
        sid = context.user_data.get("sid")
        if sid and sid in streams:
            streams[sid]["source"] = text
            save_streams()
            context.user_data["step"] = None
            # بعد الإضافة نعرض لوحة التحكم
            await show_panel(update, sid, context)
        else:
            await update.message.reply_text("❌ خطأ، حاول مرة أخرى")

    # ----- أزرار التحكم في البث الحالي -----
    elif text == "▶️ تشغيل":
        sid = context.user_data.get("current_sid")
        if not sid:
            await update.message.reply_text("❌ اختر بثاً أولاً من القائمة")
            return
        if streams[sid].get("active"):
            await update.message.reply_text("⚠️ البث يعمل بالفعل")
            return
        if not streams[sid].get("source"):
            await update.message.reply_text("❌ لا يوجد مصدر. استخدم 'تغيير المصدر' أولاً")
            return
        asyncio.create_task(start_stream(sid, chat_id, context.bot))
        await update.message.reply_text("▶️ جاري تشغيل البث...")

    elif text == "⏹ إيقاف":
        sid = context.user_data.get("current_sid")
        if sid:
            await stop_stream(sid)
            await update.message.reply_text("⏹ تم إيقاف البث")
        else:
            await update.message.reply_text("❌ لا يوجد بث نشط")

    elif text == "📥 تغيير المصدر":
        sid = context.user_data.get("current_sid")
        if not sid:
            await update.message.reply_text("❌ اختر بثاً أولاً")
            return
        context.user_data["edit"] = ("source", sid)
        await update.message.reply_text("📥 أرسل الرابط الجديد:")

    elif text == "🖼 تغيير الشعار":
        sid = context.user_data.get("current_sid")
        if not sid:
            await update.message.reply_text("❌ اختر بثاً أولاً")
            return
        context.user_data["edit"] = ("logo", sid)
        await update.message.reply_text("🖼 أرسل رابط صورة الشعار (أو /skip للتخطي):")

    elif text == "🕵️ تغيير UA":
        sid = context.user_data.get("current_sid")
        if not sid:
            await update.message.reply_text("❌ اختر بثاً أولاً")
            return
        context.user_data["edit"] = ("ua", sid)
        await update.message.reply_text("🕵️ أرسل User-Agent الجديد (أو /skip للافتراضي):")

    elif text == "✏️ إعادة تسمية":
        sid = context.user_data.get("current_sid")
        if not sid:
            await update.message.reply_text("❌ اختر بثاً أولاً")
            return
        context.user_data["edit"] = ("rename", sid)
        await update.message.reply_text("✏️ أرسل الاسم الجديد:")

    elif text == "🔄 تبديل الوضع":
        sid = context.user_data.get("current_sid")
        if not sid:
            await update.message.reply_text("❌ اختر بثاً أولاً")
            return
        old = streams[sid]["mode"]
        streams[sid]["mode"] = "encode" if old == "copy" else "copy"
        save_streams()
        await update.message.reply_text(f"⚙️ تم تبديل الوضع إلى: {'ترميز' if streams[sid]['mode']=='encode' else 'نسخ مباشر'}")
        # إعادة عرض اللوحة بعد التغيير
        await show_panel(update, sid, context)

    elif text == "🗑 حذف البث":
        sid = context.user_data.get("current_sid")
        if not sid:
            await update.message.reply_text("❌ اختر بثاً أولاً")
            return
        await stop_stream(sid)
        streams.pop(sid, None)
        save_streams()
        context.user_data.pop("current_sid", None)
        await update.message.reply_text("🗑 تم حذف البث", reply_markup=main_kb)

    elif text == "🔙 القائمة الرئيسية":
        await update.message.reply_text("القائمة الرئيسية", reply_markup=main_kb)
        context.user_data.pop("current_sid", None)

    # معالجة التعديلات (source, logo, ua, rename)
    elif context.user_data.get("edit"):
        typ, sid = context.user_data["edit"]
        if typ == "source":
            streams[sid]["source"] = text
            await update.message.reply_text("✅ تم تحديث المصدر")
        elif typ == "logo":
            streams[sid]["logo"] = "" if text == "/skip" else text
            await update.message.reply_text("✅ تم تحديث الشعار" if text != "/skip" else "✅ تم إزالة الشعار")
        elif typ == "ua":
            streams[sid]["ua"] = "ExoPlayerLib/2.18.5" if text == "/skip" else text
            await update.message.reply_text("✅ تم تحديث User-Agent")
        elif typ == "rename":
            old = streams[sid]["name"]
            streams[sid]["name"] = text
            await update.message.reply_text(f"✅ تم تغيير الاسم من {old} إلى {text}")
        save_streams()
        context.user_data.pop("edit", None)
        # إعادة عرض اللوحة بعد التعديل
        await show_panel(update, sid, context)

    else:
        # محاولة فتح لوحة بث باسم مطابق
        found = False
        for sid, s in streams.items():
            if s["name"] == text:
                await show_panel(update, sid, context)
                found = True
                break
        if not found:
            await update.message.reply_text("❌ اختر من الأزرار أو أرسل اسم بث موجود")

# =========================================
# START BOT COMMAND
# =========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return
    await update.message.reply_text(
        "🎬 **بوت البث المباشر - الإدارة الكاملة**\nاختر من القائمة:",
        reply_markup=main_kb
    )

# =========================================
# MAIN (FIXED)
# =========================================
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Start HTTP server and cleanup loop as background tasks
    loop.create_task(start_http())
    loop.create_task(clean_viewers_loop())

    # Build and run bot
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()