import asyncio
import json
import os
import time
import subprocess
import shutil
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

# ========== أزرار الرد الرئيسية ==========
reply_kb = ReplyKeyboardMarkup([
    ["📺 قائمة HLS", "📡 قائمة RTMP"],
    ["➕ إضافة بث", "🖥 حالة السيرفر"]
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
        "🎬 **بوت البث المباشر - Rplay**\nاختر من الأزرار أدناه:",
        reply_markup=reply_kb
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = update.message.text

    if text == "📺 قائمة HLS":
        if not streams:
            await update.message.reply_text("لا توجد بثوث HLS")
            return
        msg = "📺 **قائمة HLS**:\n"
        for sid, s in streams.items():
            if s.get("type") == "hls":
                status = "🟢" if s.get("active") else "🔴"
                msg += f"{status} {s['name']}\n"
        await update.message.reply_text(msg)

    elif text == "📡 قائمة RTMP":
        if not streams:
            await update.message.reply_text("لا توجد بثوث RTMP")
            return
        msg = "📡 **قائمة RTMP**:\n"
        for sid, s in streams.items():
            if s.get("type") == "rtmp":
                status = "🟢" if s.get("active") else "🔴"
                msg += f"{status} {s['name']}\n"
        await update.message.reply_text(msg)

    elif text == "➕ إضافة بث":
        context.user_data["step"] = "wait_name"
        await update.message.reply_text("📝 أرسل اسم البث الجديد:")

    elif text == "🖥 حالة السيرفر":
        load = "غير متاح"
        try:
            with open("/proc/loadavg") as f:
                load = f.read().split()[0]
        except: pass
        await update.message.reply_text(f"🖥 حمل المعالج: {load}\n📡 عدد البثوث: {len(streams)}")

    # مرحلة إضافة بث جديد
    elif context.user_data.get("step") == "wait_name":
        name = text.strip()
        sid = name.replace(" ", "_") + str(int(time.time()))
        streams[sid] = {
            "name": name,
            "source": "",
            "type": "hls",
            "mode": "copy",          # copy أو transcode
            "active": False,
            "ua": "ExoPlayerLib/2.18.5",
            "rtmp_server": "",
            "rtmp_key": ""
        }
        save()
        context.user_data["step"] = "wait_source"
        context.user_data["sid"] = sid
        await update.message.reply_text(f"✅ تم إضافة {name}\nالآن أرسل رابط المصدر (m3u8 أو مباشر):")

    elif context.user_data.get("step") == "wait_source":
        sid = context.user_data.get("sid")
        if sid and sid in streams:
            streams[sid]["source"] = text
            save()
            context.user_data["step"] = None
            # تشغيل البث تلقائياً
            asyncio.create_task(run_ffmpeg(sid, update.message.chat_id, context.bot))
            await update.message.reply_text(f"✅ تم حفظ المصدر وسيبدأ البث خلال لحظات\n🔗 رابط المشاهدة HLS:\n{BASE_URL}:{PORT}/live/{sid}/index.m3u8")
        else:
            await update.message.reply_text("خطأ، حاول مرة أخرى")

    else:
        await update.message.reply_text("استخدم الأزرار المتاحة")

# ========== تشغيل ffmpeg بجودة عالية ومقاومة ==========
async def run_ffmpeg(sid, chat_id, bot):
    s = streams[sid]
    src = s["source"]
    ua = s.get("ua", "ExoPlayerLib/2.18.5")
    mode = s.get("mode", "copy")
    typ = s["type"]

    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "index.m3u8")

    # إعدادات أساسية لـ ffmpeg (قوية ومقاومة)
    base_opts = [
        "ffmpeg", "-re",
        "-user_agent", ua,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-timeout", "10000000",
        "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "1000000",
        "-probesize", "5000000",
        "-stream_loop", "-1",   # تعيد تشغيل المصدر تلقائياً إذا انقطع (بدون حلقة خارجية)
        "-i", src
    ]

    # إعدادات الفيديو (ترميز أو نسخ)
    if mode == "copy":
        video_codec = ["-c:v", "copy"]
    else:
        # ترميز بجودة 9 ميجابت، أقصى دقة 1920x1080
        video_codec = [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-b:v", "9000k",
            "-maxrate", "9000k",
            "-bufsize", "18000k",
            "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
            "-g", "90"   # keyframe كل 3 ثوانٍ (30fps * 3)
        ]

    # إعدادات الصوت
    audio_codec = ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]

    if typ == "hls":
        cmd = base_opts + video_codec + audio_codec + [
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            out_file
        ]
    else:  # RTMP
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        cmd = base_opts + video_codec + audio_codec + ["-f", "flv", rtmp_url]

    s["active"] = True
    save()

    # تشغيل العملية – ستستمر إلى الأبد بفضل stream_loop -1
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )
    s["process"] = sid  # للإشارة

    # قراءة stderr لعرض أخطاء مهمة (اختياري)
    async def read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            decoded = line.decode(errors="ignore").strip()
            if "error" in decoded.lower() or "failed" in decoded.lower():
                print(f"[{sid}] FFmpeg error: {decoded}")
    asyncio.create_task(read_stderr())

    await proc.wait()
    # إذا وصلنا إلى هنا، فهذا يعني أن ffmpeg توقف (نادراً بسبب stream_loop -1)
    s["active"] = False
    save()
    await bot.send_message(chat_id, f"⚠️ توقف بث {s['name']} بشكل غير متوقع. جاري المحاولة مرة أخرى...")
    # إعادة التشغيل مرة واحدة فقط
    asyncio.create_task(run_ffmpeg(sid, chat_id, bot))

# ========== إيقاف البث ==========
async def stop_stream(sid, bot, chat_id):
    s = streams.get(sid)
    if not s:
        return
    # إيقاف عملية ffmpeg
    # نستخدم pkill لأننا لا نخزن كائن العملية (يمكن تحسينه)
    try:
        proc = await asyncio.create_subprocess_exec("pkill", "-f", f"ffmpeg.*{sid}", stdout=asyncio.subprocess.DEVNULL)
        await proc.wait()
    except:
        pass
    s["active"] = False
    # حذف ملفات HLS
    dir_path = os.path.join(HLS_DIR, sid)
    if os.path.exists(dir_path):
        shutil.rmtree(dir_path, ignore_errors=True)
    save()
    await bot.send_message(chat_id, f"⏹ تم إيقاف بث {s['name']}")

# ========== دالة مساعدة لإظهار لوحة التحكم ==========
async def show_panel(update, sid):
    s = streams.get(sid)
    if not s:
        await update.message.reply_text("البث غير موجود")
        return
    text = f"🎛️ **{s['name']}**\n"
    text += f"📥 المصدر: {s['source']}\n"
    text += f"⚙️ الوضع: {'نسخ مباشر' if s['mode']=='copy' else 'ترميز (جودة عالية)'}\n"
    text += f"🟢 الحالة: {'يعمل' if s.get('active') else 'متوقف'}\n"
    if s['type'] == 'hls':
        text += f"🔗 رابط المشاهدة:\n{BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    else:
        text += f"📡 RTMP: {s['rtmp_server']}/{s['rtmp_key']}"
    kb = ReplyKeyboardMarkup([
        ["▶️ تشغيل", "⏹ إيقاف"],
        ["📥 تغيير المصدر", "🔄 تبديل الوضع"],
        ["🗑 حذف البث", "🔙 القائمة"]
    ], resize_keyboard=True)
    await update.message.reply_text(text, reply_markup=kb)
    context.user_data["current_sid"] = sid

# ========== تشغيل البوت ==========
async def main():
    await start_http()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())