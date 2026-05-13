import asyncio
import json
import os
import time
import shutil
from aiohttp import web
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

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
processes = {}

if os.path.exists(STREAMS_FILE):
    with open(STREAMS_FILE) as f:
        streams = json.load(f)


def save_streams():
    with open(STREAMS_FILE, "w") as f:
        json.dump(streams, f, indent=2)


# ========== الكيبورد ==========
reply_kb = ReplyKeyboardMarkup([
    ["📺 قائمة HLS", "📡 قائمة RTMP"],
    ["➕ إضافة بث", "🖥 حالة السيرفر"]
], resize_keyboard=True)


# ========== HTTP SERVER ==========
async def hls_handler(request):
    name = request.match_info["name"]
    filename = request.match_info.get("file", "index.m3u8")

    path = os.path.join(HLS_DIR, name, filename)

    if not os.path.exists(path):
        return web.Response(status=404)

    return web.FileResponse(path)


async def start_http():
    app = web.Application()

    app.router.add_get(
        "/live/{name}/{file:.*}",
        hls_handler
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    print(f"HTTP server started on {PORT}")


# ========== START ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("غير مصرح")
        return

    await update.message.reply_text(
        "🎬 بوت البث المباشر\nاختر من القائمة:",
        reply_markup=reply_kb
    )


# ========== لوحة التحكم ==========
async def show_panel(update, sid, context):
    s = streams.get(sid)

    if not s:
        await update.message.reply_text("البث غير موجود")
        return

    txt = (
        f"🎛️ {s['name']}\n\n"
        f"📥 المصدر:\n{s['source']}\n\n"
        f"⚙️ الوضع: "
        f"{'نسخ مباشر' if s['mode']=='copy' else 'ترميز'}\n"
        f"🟢 الحالة: "
        f"{'يعمل' if s.get('active') else 'متوقف'}\n\n"
    )

    if s["type"] == "hls":
        txt += (
            f"🔗 رابط المشاهدة:\n"
            f"{BASE_URL}:{PORT}/live/{sid}/index.m3u8"
        )

    kb = ReplyKeyboardMarkup([
        ["▶️ تشغيل", "⏹ إيقاف"],
        ["🔄 تبديل الوضع", "🗑 حذف البث"],
        ["🔙 القائمة الرئيسية"]
    ], resize_keyboard=True)

    context.user_data["current_sid"] = sid

    await update.message.reply_text(
        txt,
        reply_markup=kb
    )


# ========== تشغيل البث ==========
async def start_stream(sid, chat_id, bot):
    s = streams[sid]

    src = s["source"]
    mode = s.get("mode", "copy")
    ua = s.get("ua", "ExoPlayerLib/2.18.5")

    out_dir = os.path.join(HLS_DIR, sid)

    os.makedirs(out_dir, exist_ok=True)

    out_file = os.path.join(
        out_dir,
        "index.m3u8"
    )

    base_cmd = [
        "ffmpeg",
        "-re",
        "-user_agent", ua,

        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",

        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",

        "-i", src
    ]

    if mode == "copy":
        video_opts = [
            "-c:v", "copy"
        ]
    else:
        video_opts = [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-g", "50"
        ]

    audio_opts = [
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2"
    ]

    cmd = (
        base_cmd
        + video_opts
        + audio_opts
        + [
            "-f", "hls",

            "-hls_time", "2",
            "-hls_list_size", "5",

            "-hls_flags",
            "delete_segments+append_list",

            out_file
        ]
    )

    print(" ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )

    processes[sid] = proc

    streams[sid]["active"] = True
    save_streams()

    async def read_logs():
        while True:
            line = await proc.stderr.readline()

            if not line:
                break

            txt = line.decode(
                errors="ignore"
            ).strip()

            if "error" in txt.lower():
                print(f"[{sid}] {txt}")

    asyncio.create_task(read_logs())

    await proc.wait()

    streams[sid]["active"] = False

    processes.pop(sid, None)

    save_streams()

    await bot.send_message(
        chat_id,
        f"⛔ توقف بث {s['name']}"
    )


# ========== إيقاف البث ==========
async def stop_stream(sid):
    if sid in processes:
        try:
            processes[sid].terminate()

            await asyncio.sleep(1)

            processes[sid].kill()

        except:
            pass

        processes.pop(sid, None)

    if sid in streams:
        streams[sid]["active"] = False

    save_streams()

    hls_path = os.path.join(HLS_DIR, sid)

    if os.path.exists(hls_path):
        shutil.rmtree(
            hls_path,
            ignore_errors=True
        )


# ========== الرسائل ==========
async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text

    # ===== قائمة HLS =====
    if text == "📺 قائمة HLS":
        msg = "📺 قائمة HLS:\n\n"

        found = False

        for sid, s in streams.items():
            if s["type"] == "hls":
                found = True

                status = (
                    "🟢"
                    if s.get("active")
                    else "🔴"
                )

                msg += f"{status} {s['name']}\n"

        if not found:
            msg = "لا توجد بثوث HLS"

        await update.message.reply_text(msg)

    # ===== قائمة RTMP =====
    elif text == "📡 قائمة RTMP":
        msg = "📡 قائمة RTMP:\n\n"

        found = False

        for sid, s in streams.items():
            if s["type"] == "rtmp":
                found = True

                status = (
                    "🟢"
                    if s.get("active")
                    else "🔴"
                )

                msg += f"{status} {s['name']}\n"

        if not found:
            msg = "لا توجد بثوث RTMP"

        await update.message.reply_text(msg)

    # ===== إضافة بث =====
    elif text == "➕ إضافة بث":
        context.user_data["step"] = "wait_name"

        await update.message.reply_text(
            "أرسل اسم البث:"
        )

    # ===== حالة السيرفر =====
    elif text == "🖥 حالة السيرفر":
        load = "غير معروف"

        try:
            with open("/proc/loadavg") as f:
                load = f.read().split()[0]
        except:
            pass

        await update.message.reply_text(
            f"🖥 الحمل: {load}\n"
            f"📡 عدد البثوث: {len(streams)}"
        )

    # ===== اسم البث =====
    elif context.user_data.get("step") == "wait_name":
        name = text.strip()

        sid = (
            name.replace(" ", "_")
            + str(int(time.time()))
        )

        streams[sid] = {
            "name": name,
            "source": "",
            "type": "hls",
            "mode": "copy",
            "active": False,
            "ua": "ExoPlayerLib/2.18.5"
        }

        save_streams()

        context.user_data["step"] = "wait_source"
        context.user_data["sid"] = sid

        await update.message.reply_text(
            "أرسل رابط المصدر:"
        )

    # ===== رابط المصدر =====
    elif context.user_data.get("step") == "wait_source":
        sid = context.user_data.get("sid")

        if sid and sid in streams:
            streams[sid]["source"] = text

            save_streams()

            context.user_data["step"] = None

            await update.message.reply_text(
                f"✅ تم حفظ البث\n\n"
                f"🔗 رابط المشاهدة:\n"
                f"{BASE_URL}:{PORT}/live/{sid}/index.m3u8"
            )

    # ===== تشغيل =====
    elif text == "▶️ تشغيل":
        sid = context.user_data.get("current_sid")

        if not sid:
            await update.message.reply_text(
                "اختر بث أولاً"
            )
            return

        if streams[sid].get("active"):
            await update.message.reply_text(
                "البث يعمل بالفعل"
            )
            return

        asyncio.create_task(
            start_stream(
                sid,
                update.effective_chat.id,
                context.bot
            )
        )

        await update.message.reply_text(
            "▶️ جاري تشغيل البث"
        )

    # ===== إيقاف =====
    elif text == "⏹ إيقاف":
        sid = context.user_data.get("current_sid")

        if not sid:
            return

        await stop_stream(sid)

        await update.message.reply_text(
            "⏹ تم إيقاف البث"
        )

    # ===== تبديل الوضع =====
    elif text == "🔄 تبديل الوضع":
        sid = context.user_data.get("current_sid")

        if not sid:
            return

        streams[sid]["mode"] = (
            "encode"
            if streams[sid]["mode"] == "copy"
            else "copy"
        )

        save_streams()

        await update.message.reply_text(
            f"⚙️ الوضع الحالي: "
            f"{streams[sid]['mode']}"
        )

    # ===== حذف =====
    elif text == "🗑 حذف البث":
        sid = context.user_data.get("current_sid")

        if not sid:
            return

        await stop_stream(sid)

        streams.pop(sid, None)

        save_streams()

        await update.message.reply_text(
            "🗑 تم حذف البث",
            reply_markup=reply_kb
        )

    # ===== رجوع =====
    elif text == "🔙 القائمة الرئيسية":
        await update.message.reply_text(
            "القائمة الرئيسية",
            reply_markup=reply_kb
        )

    # ===== فتح لوحة بث =====
    else:
        for sid, s in streams.items():
            if s["name"] == text:
                await show_panel(
                    update,
                    sid,
                    context
                )
                return

        await update.message.reply_text(
            "استخدم الأزرار المتاحة"
        )


# ========== MAIN ==========
async def main():
    await start_http()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    print("Bot started...")

    await app.initialize()

    await app.start()

    await app.updater.start_polling()

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())