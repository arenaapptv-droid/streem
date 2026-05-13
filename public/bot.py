import asyncio
import json
import os
import re
import time
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ========== تحميل الإعدادات ==========
with open("settings.json") as f:
    cfg = json.load(f)
TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]

# إعدادات الجودة العالية
VIDEO_BITRATE = cfg.get("VIDEO_BITRATE", "6000k")   # مثلاً 6000k أو 9000k
MAXRATE = cfg.get("MAXRATE", "9000k")
BUFSIZE = cfg.get("BUFSIZE", "18000k")
AUDIO_BITRATE = cfg.get("AUDIO_BITRATE", "128k")
PRESET = cfg.get("PRESET", "faster")  # faster, veryfast, medium

# ========== إعدادات البث ==========
HLS_DIR = "/tmp/hls"
os.makedirs(HLS_DIR, exist_ok=True)
PORT = 8080
BASE_URL = "http://164.68.102.28"
STREAMS_FILE = "streams.json"

streams = {}

def load_streams():
    global streams
    if os.path.exists(STREAMS_FILE):
        with open(STREAMS_FILE) as f:
            streams = json.load(f)
        for sid in streams:
            streams[sid]["process"] = None
            streams[sid].setdefault("mode", "transcode")
            streams[sid].setdefault("type", "hls")
            streams[sid].setdefault("rtmp_server", "")
            streams[sid].setdefault("rtmp_key", "")
            streams[sid].setdefault("user_agent", "")
            streams[sid].setdefault("logo", "")

def save_streams():
    data = {sid: {k: v for k, v in s.items() if k != "process"} for sid, s in streams.items()}
    with open(STREAMS_FILE, "w") as f:
        json.dump(data, f, indent=2)

load_streams()

# ========== خادم HLS ==========
async def hls_handler(request):
    name = request.match_info["name"]
    filename = request.match_info.get("file", "index.m3u8")
    path = os.path.join(HLS_DIR, name, filename)
    if not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path, headers={
        "Content-Type": "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
    })

async def start_http():
    app = web.Application()
    app.router.add_get("/live/{name}/{file:.*}", hls_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

# ========== واجهة البوت ==========
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 قائمة HLS", callback_data="list_hls")],
        [InlineKeyboardButton("📡 قائمة RTMP", callback_data="list_rtmp")],
        [InlineKeyboardButton("➕ إضافة بث", callback_data="add")],
        [InlineKeyboardButton("🖥 حالة السيرفر", callback_data="status")]
    ])

def stream_list(typ):
    kb = []
    for sid, s in streams.items():
        if s["type"] != typ:
            continue
        status = "🟢" if s.get("active") else "⏹"
        kb.append([InlineKeyboardButton(f"{status} {s['name']}", callback_data=f"panel_{sid}")])
    kb.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu")])
    return InlineKeyboardMarkup(kb)

def panel_buttons(sid, s):
    active = s.get("active", False)
    mode = s.get("mode", "transcode")
    kb = []
    # صف التشغيل/الإيقاف
    if active:
        kb.append([InlineKeyboardButton("⏹ إيقاف البث", callback_data=f"stop_{sid}")])
    else:
        kb.append([InlineKeyboardButton("▶️ تشغيل البث", callback_data=f"start_{sid}")])
    # صف المصدر والشعار
    kb.append([
        InlineKeyboardButton("📥 تعيين المصدر", callback_data=f"src_{sid}"),
        InlineKeyboardButton("🖼 تعيين الشعار", callback_data=f"logo_{sid}")
    ])
    # صف UA وإعادة التسمية
    kb.append([
        InlineKeyboardButton("🕵️ تعيين UA", callback_data=f"ua_{sid}"),
        InlineKeyboardButton("✏️ إعادة تسمية", callback_data=f"rename_{sid}")
    ])
    # إعدادات RTMP إن وجدت
    if s["type"] == "rtmp":
        kb.append([
            InlineKeyboardButton("📡 خادم RTMP", callback_data=f"rtmp_srv_{sid}"),
            InlineKeyboardButton("🔑 مفتاح RTMP", callback_data=f"rtmp_key_{sid}")
        ])
    # تبديل وضع التشغيل
    kb.append([InlineKeyboardButton(
        "🔄 وضع النسخ المباشر" if mode == "transcode" else "⚙️ وضع الترميز (جودة عالية)",
        callback_data=f"mode_{sid}"
    )])
    # حذف البث
    kb.append([InlineKeyboardButton("🗑 حذف البث", callback_data=f"del_{sid}")])
    # معلومات إضافية إذا كان البث نشطاً
    if active:
        uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s.get("start_time", 0)))
        kb.append([InlineKeyboardButton(f"⏱ مدة التشغيل: {uptime}", callback_data="noop")])
    # زر العودة للقائمة
    kb.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu")])
    return InlineKeyboardMarkup(kb)

async def show_panel(sid, bot, chat_id, msg_id):
    s = streams.get(sid)
    if not s:
        return
    text = f"🎛️ **{s['name']}** ({s['type'].upper()})\n"
    text += f"📥 المصدر: {s.get('source', 'غير محدد')}\n"
    text += f"🖼 الشعار: {'✅ موجود' if s.get('logo') else '❌ لا يوجد'}\n"
    text += f"🕵️ User-Agent: {s.get('user_agent', 'افتراضي')}\n"
    if s["type"] == "rtmp":
        text += f"📡 RTMP: {s.get('rtmp_server', 'غير محدد')}/{s.get('rtmp_key', 'غير محدد')}\n"
    else:
        text += f"🔗 رابط المشاهدة:\n{BASE_URL}:{PORT}/live/{sid}/index.m3u8\n"
    try:
        await bot.edit_message_text(text, chat_id, msg_id, reply_markup=panel_buttons(sid, s))
    except Exception as e:
        if "Message is not modified" not in str(e):
            print(f"Panel error: {e}")

# ========== تشغيل البث ==========
async def run_ffmpeg(sid, bot, chat_id, msg_id):
    s = streams[sid]
    src = s["source"]
    logo = s.get("logo", "")
    ua = s.get("user_agent", "ExoPlayerLib/2.18.5")
    mode = s["mode"]
    typ = s["type"]

    # إعداد مجلد HLS
    out_dir = os.path.join(HLS_DIR, sid)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "index.m3u8")

    # الأوامر الأساسية لـ ffmpeg
    base_cmd = [
        "ffmpeg", "-re", "-threads", "2",
        "-user_agent", ua,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
        "-timeout", "10000000", "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "1000000", "-probesize", "1000000",
        "-i", src
    ]

    if typ == "rtmp":
        dst = f"{s['rtmp_server']}/{s['rtmp_key']}"
        if mode == "copy":
            cmd = base_cmd + ["-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-f", "flv", dst]
        else:
            # وضع الترميز (جودة عالية)
            filter_cmd = []
            if logo:
                filter_cmd = ["-i", logo, "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0"]
            cmd = base_cmd + filter_cmd + [
                "-c:v", "libx264", "-preset", PRESET, "-crf", "23",
                "-b:v", VIDEO_BITRATE, "-maxrate", MAXRATE, "-bufsize", BUFSIZE,
                "-g", "90",  # keyframe كل 3 ثوانٍ (30fps * 3)
                "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2",
                "-f", "flv", dst
            ]
    else:  # HLS
        if mode == "copy":
            cmd = base_cmd + [
                "-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", out_file
            ]
        else:
            filter_cmd = []
            if logo:
                filter_cmd = ["-i", logo, "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo];[logo][ref]overlay=0:0"]
            cmd = base_cmd + filter_cmd + [
                "-c:v", "libx264", "-preset", PRESET, "-crf", "23",
                "-b:v", VIDEO_BITRATE, "-maxrate", MAXRATE, "-bufsize", BUFSIZE,
                "-g", "90",
                "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", out_file
            ]

    s["active"] = True
    s["start_time"] = time.time()
    save_streams()

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    s["process"] = proc

    # قراءة stderr لعرض FPS فقط (اختياري)
    async def read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            decoded = line.decode(errors="ignore").strip()
            if "fps=" in decoded:
                m = re.search(r"fps=\s*([\d.]+)", decoded)
                if m:
                    s["fps"] = m.group(1)
    asyncio.create_task(read_stderr())

    # مراقبة البث (تحديث اللوحة كل 5 ثوانٍ)
    while proc.returncode is None:
        await asyncio.sleep(5)
        await show_panel(sid, bot, chat_id, msg_id)

    # انتهى البث بشكل غير متوقع
    s["active"] = False
    s["process"] = None
    save_streams()
    await show_panel(sid, bot, chat_id, msg_id)
    print(f"Stream {sid} ended. Return code: {proc.returncode}")

# ========== معالجات الأزرار والرسائل ==========
async def start(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return
    await update.message.reply_text("مرحبًا بك في بوت البث المباشر (جودة عالية)", reply_markup=main_menu())

async def button_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    if update.effective_user.id != ADMIN_ID:
        await query.answer("غير مصرح", show_alert=True)
        return
    chat_id = query.message.chat_id
    msg_id = query.message.message_id

    if data == "menu":
        await query.edit_message_text("القائمة الرئيسية", reply_markup=main_menu())
    elif data == "list_hls":
        await query.edit_message_text("📺 قائمة بثوث HLS", reply_markup=stream_list("hls"))
    elif data == "list_rtmp":
        await query.edit_message_text("📡 قائمة بثوث RTMP", reply_markup=stream_list("rtmp"))
    elif data == "add":
        context.user_data["step"] = "add_name"
        await query.edit_message_text("📝 أرسل اسم البث الجديد:")
    elif data == "status":
        # إحصائيات بسيطة
        with open("/proc/loadavg") as f:
            load = f.read().split()[0]
        with open("/proc/meminfo") as f:
            total_mem = int([l for l in f if "MemTotal" in l][0].split()[1]) // 1024
        with open("/proc/meminfo") as f:
            avail_mem = int([l for l in f if "MemAvailable" in l][0].split()[1]) // 1024
        status = f"🖥 حمل المعالج: {load}\n💾 الذاكرة: {total_mem - avail_mem}/{total_mem} MB"
        await query.edit_message_text(status, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="menu")]]))
    elif data.startswith("panel_"):
        sid = data[6:]
        if sid in streams:
            await show_panel(sid, context.bot, chat_id, msg_id)
    elif data.startswith("start_"):
        sid = data[6:]
        s = streams.get(sid)
        if not s or not s.get("source"):
            await query.answer("❌ لا يوجد مصدر! قم بتعيين المصدر أولاً", show_alert=True)
            return
        if s["type"] == "rtmp" and (not s.get("rtmp_server") or not s.get("rtmp_key")):
            await query.answer("❌ قم بتعيين خادم RTMP والمفتاح أولاً", show_alert=True)
            return
        asyncio.create_task(run_ffmpeg(sid, context.bot, chat_id, msg_id))
        await query.answer("⏳ جاري تشغيل البث...")
    elif data.startswith("stop_"):
        sid = data[5:]
        s = streams.get(sid)
        if s and s.get("process"):
            try:
                s["process"].kill()
            except:
                pass
            s["active"] = False
            s["process"] = None
            save_streams()
        await query.answer("⏹ تم إيقاف البث")
        await show_panel(sid, context.bot, chat_id, msg_id)
    elif data.startswith("src_"):
        sid = data[4:]
        context.user_data["edit"] = ("source", sid, chat_id, msg_id)
        await query.edit_message_text("📥 أرسل رابط المصدر (يجب أن يكون رابط مباشر للفيديو أو m3u8):")
    elif data.startswith("logo_"):
        sid = data[5:]
        context.user_data["edit"] = ("logo", sid, chat_id, msg_id)
        await query.edit_message_text("🖼 أرسل رابط صورة الشعار (أو /skip للتخطي):")
    elif data.startswith("ua_"):
        sid = data[3:]
        context.user_data["edit"] = ("ua", sid, chat_id, msg_id)
        await query.edit_message_text("🕵️ أرسل User-Agent المطلوب (أو /skip للافتراضي):")
    elif data.startswith("rename_"):
        sid = data[7:]
        context.user_data["edit"] = ("name", sid, chat_id, msg_id)
        await query.edit_message_text("✏️ أرسل الاسم الجديد للبث:")
    elif data.startswith("rtmp_srv_"):
        sid = data[9:]
        context.user_data["edit"] = ("rtmp_server", sid, chat_id, msg_id)
        await query.edit_message_text("📡 أرسل رابط خادم RTMP (مثال: rtmp://example.com/live):")
    elif data.startswith("rtmp_key_"):
        sid = data[9:]
        context.user_data["edit"] = ("rtmp_key", sid, chat_id, msg_id)
        await query.edit_message_text("🔑 أرسل مفتاح البث RTMP:")
    elif data.startswith("mode_"):
        sid = data[5:]
        s = streams.get(sid)
        if s:
            old_mode = s.get("mode", "transcode")
            new_mode = "copy" if old_mode == "transcode" else "transcode"
            s["mode"] = new_mode
            save_streams()
            if s.get("active"):
                # إعادة تشغيل البث للتبديل بين الوضعين
                if s.get("process"):
                    try: s["process"].kill()
                    except: pass
                asyncio.create_task(run_ffmpeg(sid, context.bot, chat_id, msg_id))
                await query.answer(f"✅ تم التبديل إلى وضع {'النسخ' if new_mode == 'copy' else 'الترميز'} وإعادة التشغيل...")
            else:
                await query.answer(f"✅ تم التبديل إلى وضع {'النسخ' if new_mode == 'copy' else 'الترميز'}")
            await show_panel(sid, context.bot, chat_id, msg_id)
    elif data.startswith("del_"):
        sid = data[4:]
        if sid in streams:
            if streams[sid].get("process"):
                try: streams[sid]["process"].kill()
                except: pass
            del streams[sid]
            save_streams()
        await query.edit_message_text("✅ تم حذف البث نهائياً", reply_markup=main_menu())

async def message_handler(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return
    text = update.message.text.strip()
    # مرحلة إضافة بث جديد
    if context.user_data.get("step") == "add_name":
        name = text
        sid = name.replace(" ", "_")
        if sid in streams:
            sid = f"{sid}_{int(time.time())}"
        streams[sid] = {
            "name": name, "source": "", "logo": "", "user_agent": "",
            "type": "hls", "mode": "transcode", "rtmp_server": "", "rtmp_key": "",
            "active": False, "process": None
        }
        save_streams()
        context.user_data["step"] = None
        await update.message.reply_text(f"✅ تم إضافة البث **{name}**. الآن اختر نوع البث:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📺 HLS", callback_data=f"settype_{sid}_hls")],
            [InlineKeyboardButton("📡 RTMP", callback_data=f"settype_{sid}_rtmp")]
        ]))
        return
    # مرحلة تعديل بيانات البث
    if context.user_data.get("edit"):
        typ, sid, chat_id, msg_id = context.user_data["edit"]
        s = streams.get(sid)
        if not s:
            await update.message.reply_text("❌ البث غير موجود")
            context.user_data["edit"] = None
            return
        if typ == "source":
            s["source"] = text
        elif typ == "logo":
            if text != "/skip":
                s["logo"] = text
        elif typ == "ua":
            if text != "/skip":
                s["user_agent"] = text
        elif typ == "name":
            s["name"] = text
        elif typ == "rtmp_server":
            s["rtmp_server"] = text
        elif typ == "rtmp_key":
            s["rtmp_key"] = text
        save_streams()
        context.user_data["edit"] = None
        await update.message.reply_text("✅ تم الحفظ بنجاح")
        await show_panel(sid, context.bot, chat_id, msg_id)
        return

async def set_type_callback(update, context):
    query = update.callback_query
    await query.answer()
    _, sid, typ = query.data.split("_")
    if sid in streams:
        streams[sid]["type"] = typ
        save_streams()
    await query.edit_message_text(f"✅ تم تعيين نوع البث إلى {typ.upper()} للقناة **{streams[sid]['name']}**", reply_markup=main_menu())

# ========== التشغيل الرئيسي ==========
async def main():
    # تشغيل خادم HLS
    await start_http()
    # تشغيل البوت
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^(?!settype_).*"))
    app.add_handler(CallbackQueryHandler(set_type_callback, pattern="^settype_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())