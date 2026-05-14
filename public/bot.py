import asyncio
import json
import os
import re
import shutil
import time
from collections import defaultdict

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import BadRequest

# ========== الإعدادات الأساسية ==========
with open("settings.json") as f:
    cfg = json.load(f)
TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]
BASE_URL = cfg.get("BASE_URL", "http://164.68.102.28")
PORT = cfg.get("PORT", 8080)

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
    for sid, s in streams.items():
        if "viewers" in s and isinstance(s["viewers"], list):
            s["viewers"] = set(s["viewers"])
        s.setdefault("fps", "?")
        s.setdefault("logo", "")
        s.setdefault("ua", "ExoPlayerLib/2.18.5")
        s.setdefault("rtmp_server", "")
        s.setdefault("rtmp_key", "")
        s.setdefault("type", "hls")
        s.setdefault("mode", "copy")
        s.setdefault("message_id", None)
        s.setdefault("chat_id", None)
        s.setdefault("start_time", 0)

def save():
    data = {}
    for sid, s in streams.items():
        tmp = s.copy()
        if "viewers" in tmp and isinstance(tmp["viewers"], set):
            tmp["viewers"] = list(tmp["viewers"])
        tmp.pop("process", None)
        data[sid] = tmp
    with open(STREAMS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ========== خادم HLS ==========
async def hls_handler(request):
    sid = request.match_info["name"]
    filename = request.match_info.get("file", "index.m3u8")
    path = os.path.join(HLS_DIR, sid, filename)
    if not os.path.exists(path):
        return web.Response(status=404)
    ip = request.remote
    if ip:
        viewers[sid].add(ip)
        viewer_last[sid][ip] = time.time()
    return web.FileResponse(path)

async def start_http():
    app = web.Application()
    app.router.add_get("/live/{name}/{file:.*}", hls_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"HTTP on {PORT}")

# ========== حالة النظام ==========
def system_status():
    try:
        import psutil
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return f"🖥️ CPU: {cpu}%\n🧠 RAM: {mem.percent}%\n💾 DISK: {disk.percent}%\n📡 البثوث: {len(streams)}"
    except:
        return f"📡 البثوث: {len(streams)}"

# ========== مراقبة السيرفر (تحديث كل 2 ثانية) ==========
monitor_active = False
monitor_task = None

async def monitor_loop(bot, chat_id, msg_id):
    global monitor_active
    while monitor_active:
        status = system_status()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف المراقبة", callback_data="stop_monitor")]])
        try:
            await bot.edit_message_text(status, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
        except:
            pass
        await asyncio.sleep(2)

# ========== واجهة المستخدم ==========
# أزرار الرد الرئيسية (ReplyKeyboardMarkup)
reply_kb = ReplyKeyboardMarkup([
    ["📺 HLS", "📡 RTMP"],
    ["➕ إضافة", "🖥 مراقبة"],
    ["🧹 تنظيف"]
], resize_keyboard=True)

# قوائم البثوث المضمنة (InlineKeyboard) – بدون أزرار رئيسية
def streams_list(typ):
    kb = []
    for sid, s in streams.items():
        if s["type"] == typ:
            status = "🟢" if s.get("active") else "🔴"
            kb.append([InlineKeyboardButton(f"{status} {s['name']}", callback_data=f"open_{sid}")])
    if not kb:
        kb.append([InlineKeyboardButton("❌ لا توجد بثوث", callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

# لوحة تحكم البث (InlineKeyboard)
def panel_keyboard(sid, s):
    active = s.get("active", False)
    mode = s.get("mode", "copy")
    typ = s.get("type", "hls")
    kb = []
    if active:
        kb.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{sid}")])
    else:
        kb.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{sid}")])
    kb.append([
        InlineKeyboardButton("📥 مصدر", callback_data=f"source_{sid}"),
        InlineKeyboardButton("🖼 شعار", callback_data=f"logo_{sid}")
    ])
    kb.append([
        InlineKeyboardButton("🕵️ UA", callback_data=f"ua_{sid}"),
        InlineKeyboardButton("✏️ إعادة تسمية", callback_data=f"rename_{sid}")
    ])
    if typ == "rtmp":
        kb.append([
            InlineKeyboardButton("📡 خادم RTMP", callback_data=f"rtmpsrv_{sid}"),
            InlineKeyboardButton("🔑 مفتاح RTMP", callback_data=f"rtmpkey_{sid}")
        ])
    toggle = "🔄 نسخ مباشر" if mode == "encode" else "⚙️ ترميز"
    kb.append([InlineKeyboardButton(toggle, callback_data=f"mode_{sid}")])
    kb.append([InlineKeyboardButton("🗑 حذف", callback_data=f"del_{sid}")])
    if active:
        viewers_count = len(viewers.get(sid, set()))
        uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
        mode_label = "نسخ" if mode == "copy" else "ترميز"
        info = f"FPS:{s.get('fps','?')} | 👥{viewers_count} | ⏱️{uptime} | {mode_label}"
        kb.append([InlineKeyboardButton(info, callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

async def update_panel(sid, bot):
    s = streams.get(sid)
    if not s or not s.get("chat_id") or not s.get("message_id"):
        return
    text = (
        f"🎛️ **{s['name']}**\n"
        f"📥 المصدر: `{s['source']}`\n"
        f"🖼 الشعار: {'✅' if s.get('logo') else '❌'}\n"
        f"🕵️ UA: `{s.get('ua')}`\n"
        f"⚙️ الوضع: {'نسخ مباشر' if s['mode']=='copy' else 'ترميز'}\n"
        f"🟢 الحالة: {'يعمل' if s.get('active') else 'متوقف'}\n"
        f"🎬 FPS: {s.get('fps','?')}\n"
        f"👥 المشاهدين: {len(viewers.get(sid, set()))}\n"
        f"⏱️ التشغيل: {time.strftime('%H:%M:%S', time.gmtime(time.time() - s['start_time'])) if s.get('start_time') else '00:00:00'}\n"
    )
    if s["type"] == "hls":
        text += f"\n🔗 {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    else:
        text += f"\n📡 {s.get('rtmp_server')}/{s.get('rtmp_key')}"
    try:
        await bot.edit_message_text(
            chat_id=s["chat_id"],
            message_id=s["message_id"],
            text=text,
            reply_markup=panel_keyboard(sid, s),
            parse_mode="Markdown"
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            print(f"Panel update error: {e}")

def get_panel_text(sid):
    s = streams.get(sid)
    if not s:
        return "❌ البث غير موجود"
    uptime = "00:00:00"
    if s.get("start_time"):
        uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
    viewers_count = len(viewers.get(sid, set()))
    text = (
        f"🎛️ **{s['name']}**\n"
        f"📥 المصدر: `{s['source']}`\n"
        f"🖼 الشعار: {'✅' if s.get('logo') else '❌'}\n"
        f"🕵️ UA: `{s.get('ua')}`\n"
        f"⚙️ الوضع: {'نسخ مباشر' if s['mode']=='copy' else 'ترميز'}\n"
        f"🟢 الحالة: {'يعمل' if s.get('active') else 'متوقف'}\n"
        f"🎬 FPS: {s.get('fps','?')}\n"
        f"👥 المشاهدين: {viewers_count}\n"
        f"⏱️ التشغيل: {uptime}\n"
    )
    if s["type"] == "hls":
        text += f"\n🔗 {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    else:
        text += f"\n📡 {s.get('rtmp_server')}/{s.get('rtmp_key')}"
    return text

# ========== تشغيل البث (أوامر مستقرة ومقاومة للأخطاء) ==========
async def start_stream(sid, bot):
    s = streams[sid]
    src = s["source"]
    mode = s.get("mode", "copy")
    ua = s.get("ua", "ExoPlayerLib/2.18.5")
    logo = s.get("logo", "")
    typ = s["type"]

    out_dir = os.path.join(HLS_DIR, sid)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "index.m3u8")

    # خيارات إعادة الاتصال المتقدمة والتحميل المسبق
    base_opts = [
        "ffmpeg", "-re",
        "-user_agent", ua,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-timeout", "10000000", "-rw_timeout", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "5000000", "-probesize", "50000000",
        "-stream_loop", "-1"
    ]

    if typ == "hls":
        if mode == "copy":
            cmd = base_opts + ["-i", src] + [
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", "-y", out_file
            ]
        else:
            if logo and len(logo) > 5:
                cmd = base_opts + ["-i", src, "-i", logo, "-filter_complex",
                                   "[1:v][0:v] scale2ref=iw:ih [logo][ref]; [ref][logo] overlay=0:0"] + [
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
                    "-vsync", "cfr", "-r", "30", "-g", "90",
                    "-c:a", "aac", "-b:a", "128k",
                    "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                    "-hls_flags", "delete_segments", "-y", out_file
                ]
            else:
                cmd = base_opts + ["-i", src] + [
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
                    "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
                    "-vsync", "cfr", "-r", "30", "-g", "90",
                    "-c:a", "aac", "-b:a", "128k",
                    "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                    "-hls_flags", "delete_segments", "-y", out_file
                ]
    else:  # RTMP
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        if mode == "copy":
            cmd = base_opts + ["-i", src] + [
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-f", "flv", "-y", rtmp_url
            ]
        else:
            if logo and len(logo) > 5:
                cmd = base_opts + ["-i", src, "-i", logo, "-filter_complex",
                                   "[1:v][0:v] scale2ref=iw:ih [logo][ref]; [ref][logo] overlay=0:0"] + [
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
                    "-vsync", "cfr", "-r", "30", "-g", "90",
                    "-c:a", "aac", "-b:a", "128k",
                    "-f", "flv", "-y", rtmp_url
                ]
            else:
                cmd = base_opts + ["-i", src] + [
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
                    "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
                    "-vsync", "cfr", "-r", "30", "-g", "90",
                    "-c:a", "aac", "-b:a", "128k",
                    "-f", "flv", "-y", rtmp_url
                ]

    if sid in processes:
        try:
            processes[sid].terminate()
            await asyncio.sleep(0.5)
            processes[sid].kill()
        except: pass

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )
    processes[sid] = proc
    s["active"] = True
    s["start_time"] = time.time()
    save()
    await update_panel(sid, bot)

    async def read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line: break
            txt = line.decode(errors="ignore").strip()
            m = re.search(r"fps=\s*([\d.]+)", txt)
            if m:
                s["fps"] = m.group(1)
                await update_panel(sid, bot)
            if "error" in txt.lower() and "deprecated" not in txt.lower():
                print(f"[{sid}] {txt}")
    asyncio.create_task(read_stderr())
    await proc.wait()

    s["active"] = False
    processes.pop(sid, None)
    save()
    await update_panel(sid, bot)

async def stop_stream(sid, bot):
    if sid in processes:
        try:
            processes[sid].terminate()
            await asyncio.sleep(0.5)
            processes[sid].kill()
        except: pass
        processes.pop(sid, None)
    if sid in streams:
        streams[sid]["active"] = False
        save()
    shutil.rmtree(os.path.join(HLS_DIR, sid), ignore_errors=True)
    await update_panel(sid, bot)

# ========== معالجات الأزرار ==========
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_active, monitor_task
    q = update.callback_query
    try: await q.answer()
    except: pass
    data = q.data
    chat_id = q.message.chat_id
    msg_id = q.message.message_id

    if data == "main_menu":
        if monitor_active:
            monitor_active = False
            if monitor_task: monitor_task.cancel()
        await q.edit_message_text("🎬 القائمة الرئيسية", reply_markup=inline_main_menu())
        return

    if data == "stop_monitor":
        if monitor_active:
            monitor_active = False
            if monitor_task: monitor_task.cancel()
        await q.edit_message_text(system_status(), reply_markup=inline_main_menu())
        return

    if data == "list_hls":
        await q.edit_message_text("📺 قائمة HLS:", reply_markup=streams_list("hls"))
        return
    if data == "list_rtmp":
        await q.edit_message_text("📡 قائمة RTMP:", reply_markup=streams_list("rtmp"))
        return

    if data.startswith("open_"):
        sid = data[5:]
        if sid in streams:
            streams[sid]["chat_id"] = chat_id
            streams[sid]["message_id"] = msg_id
            save()
            await update_panel(sid, context.bot)
        else:
            await q.edit_message_text("❌ البث غير موجود")
        return

    if data.startswith("start_"):
        sid = data[6:]
        if sid in streams:
            s = streams[sid]
            if not s.get("source"):
                await q.answer("❌ لا يوجد مصدر!", show_alert=True)
                return
            if s["type"] == "rtmp" and (not s.get("rtmp_server") or not s.get("rtmp_key")):
                await q.answer("❌ إعدادات RTMP غير مكتملة!", show_alert=True)
                return
            if s.get("active"):
                await q.answer("⚠️ البث يعمل بالفعل", show_alert=True)
                return
            await q.answer("⏳ جاري التشغيل...")
            asyncio.create_task(start_stream(sid, context.bot))
        else:
            await q.answer("❌ خطأ", show_alert=True)
        return

    if data.startswith("stop_"):
        sid = data[5:]
        await q.answer("⏹ تم الإيقاف")
        asyncio.create_task(stop_stream(sid, context.bot))
        return

    if data.startswith("source_"):
        sid = data[7:]
        context.user_data["edit"] = ("source", sid, chat_id, msg_id)
        await q.edit_message_text("📥 أرسل رابط المصدر الجديد:")
        return
    if data.startswith("logo_"):
        sid = data[5:]
        context.user_data["edit"] = ("logo", sid, chat_id, msg_id)
        await q.edit_message_text("🖼 أرسل رابط الشعار (أو /skip):")
        return
    if data.startswith("ua_"):
        sid = data[3:]
        context.user_data["edit"] = ("ua", sid, chat_id, msg_id)
        await q.edit_message_text("🕵️ أرسل User-Agent (أو /skip):")
        return
    if data.startswith("rename_"):
        sid = data[7:]
        context.user_data["edit"] = ("rename", sid, chat_id, msg_id)
        await q.edit_message_text("✏️ أرسل الاسم الجديد:")
        return
    if data.startswith("rtmpsrv_"):
        sid = data[8:]
        context.user_data["edit"] = ("rtmp_server", sid, chat_id, msg_id)
        await q.edit_message_text("📡 أرسل خادم RTMP:")
        return
    if data.startswith("rtmpkey_"):
        sid = data[8:]
        context.user_data["edit"] = ("rtmp_key", sid, chat_id, msg_id)
        await q.edit_message_text("🔑 أرسل مفتاح RTMP:")
        return

    if data.startswith("mode_"):
        sid = data[5:]
        if sid in streams:
            old = streams[sid]["mode"]
            new = "encode" if old == "copy" else "copy"
            streams[sid]["mode"] = new
            save()
            await q.answer(f"✅ تم التبديل إلى {'ترميز' if new=='encode' else 'نسخ'}")
            if streams[sid].get("active"):
                await stop_stream(sid, context.bot)
                asyncio.create_task(start_stream(sid, context.bot))
            else:
                await update_panel(sid, context.bot)
        else:
            await q.answer("❌ خطأ", show_alert=True)
        return

    if data.startswith("del_"):
        sid = data[4:]
        await stop_stream(sid, context.bot)
        if sid in streams:
            del streams[sid]
            save()
        await q.edit_message_text("🗑 تم حذف البث", reply_markup=inline_main_menu())
        return

def inline_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 قائمة HLS", callback_data="list_hls")],
        [InlineKeyboardButton("📡 قائمة RTMP", callback_data="list_rtmp")],
        [InlineKeyboardButton("➕ إضافة بث", callback_data="add_stream")],
        [InlineKeyboardButton("🖥 مراقبة السيرفر", callback_data="monitor")],
        [InlineKeyboardButton("🧹 تنظيف الملفات", callback_data="clean")]
    ])

# ========== معالجة الرسائل النصية ==========
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_active, monitor_task
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return
    text = update.message.text
    chat_id = update.message.chat_id

    # أزرار الرد الرئيسية
    if text == "📺 HLS":
        await update.message.reply_text("📺 قائمة HLS:", reply_markup=streams_list("hls"))
        return
    if text == "📡 RTMP":
        await update.message.reply_text("📡 قائمة RTMP:", reply_markup=streams_list("rtmp"))
        return
    if text == "➕ إضافة":
        context.user_data["step"] = "add_name"
        await update.message.reply_text("📝 أرسل اسم البث الجديد:")
        return
    if text == "🖥 مراقبة":
        if monitor_active:
            monitor_active = False
            if monitor_task: monitor_task.cancel()
            await update.message.reply_text(system_status(), reply_markup=reply_kb)
            return
        status = system_status()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف المراقبة", callback_data="stop_monitor")]])
        msg = await update.message.reply_text(status, reply_markup=kb)
        monitor_active = True
        monitor_task = asyncio.create_task(monitor_loop(context.bot, msg.chat_id, msg.message_id))
        return
    if text == "🧹 تنظيف":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        await update.message.reply_text("✅ تم تنظيف مجلد HLS")
        return

    # إضافة بث جديد: الاسم
    if context.user_data.get("step") == "add_name":
        name = text.strip()
        sid = name.replace(" ", "_")
        if sid in streams:
            c = 1
            while f"{sid}_{c}" in streams:
                c += 1
            sid = f"{sid}_{c}"
        streams[sid] = {
            "name": name, "source": "", "type": "hls", "mode": "copy", "active": False,
            "fps": "?", "ua": "ExoPlayerLib/2.18.5", "logo": "", "rtmp_server": "",
            "rtmp_key": "", "chat_id": None, "message_id": None, "start_time": 0
        }
        save()
        context.user_data["step"] = "add_source"
        context.user_data["sid"] = sid
        await update.message.reply_text("📥 أرسل رابط المصدر:")
        return

    # إضافة بث جديد: المصدر
    if context.user_data.get("step") == "add_source":
        sid = context.user_data.get("sid")
        if sid and sid in streams:
            streams[sid]["source"] = text
            save()
            context.user_data.pop("step")
            context.user_data.pop("sid")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("HLS", callback_data=f"settype_{sid}_hls")],
                [InlineKeyboardButton("RTMP", callback_data=f"settype_{sid}_rtmp")]
            ])
            await update.message.reply_text("اختر نوع البث:", reply_markup=kb)
        else:
            await update.message.reply_text("❌ خطأ")
        return

    # تعديل بيانات بث موجود
    if context.user_data.get("edit"):
        typ, sid, edit_chat, edit_msg = context.user_data["edit"]
        s = streams.get(sid)
        if s:
            if typ == "source": s["source"] = text
            elif typ == "logo": s["logo"] = "" if text == "/skip" else text
            elif typ == "ua": s["ua"] = "ExoPlayerLib/2.18.5" if text == "/skip" else text
            elif typ == "rename": s["name"] = text
            elif typ == "rtmp_server": s["rtmp_server"] = text
            elif typ == "rtmp_key": s["rtmp_key"] = text
            save()
            context.user_data.pop("edit")
            await update_panel(sid, context.bot)
            await update.message.delete()
        return

    # إذا كتب المستخدم اسم بث موجود، نفتح لوحة تحكم له
    for sid, s in streams.items():
        if s["name"] == text:
            if monitor_active:
                monitor_active = False
                if monitor_task: monitor_task.cancel()
            await update.message.reply_text(
                get_panel_text(sid),
                reply_markup=panel_keyboard(sid, s),
                parse_mode="Markdown"
            )
            return
    await update.message.reply_text("❌ اختر من الأزرار أو أرسل اسم بث موجود")

# ========== تحديد نوع البث ==========
async def set_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, sid, typ = q.data.split("_", 2)
    if sid in streams:
        streams[sid]["type"] = typ
        if typ == "rtmp":
            context.user_data["edit"] = ("rtmp_server", sid, q.message.chat_id, q.message.message_id)
            await q.edit_message_text("📡 أرسل خادم RTMP:")
        else:
            save()
            await update_panel(sid, context.bot)
    else:
        await q.edit_message_text("❌ خطأ")

# ========== أمر البدء ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return
    await update.message.reply_text(
        "🎬 **بوت البث المباشر - الإدارة الكاملة**\n\n"
        "📌 الأزرار السفلية للقوائم.\n"
        "➕ إضافة بث جديد.\n"
        "📺 HLS: بث لمتصفحات / أجهزة.\n"
        "📡 RTMP: بث لـ YouTube / Facebook / Twitch.\n"
        "⚙️ الترميز: جودة عالية (1080p, 9000k).\n\n"
        "استخدم الأزرار أدناه:",
        reply_markup=reply_kb
    )

# ========== التشغيل ==========
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_http())

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(callback_handler, pattern="^(?!settype_).*"))
    app.add_handler(CallbackQueryHandler(set_type_callback, pattern="^settype_"))

    print("🚀 البوت يعمل...")
    app.run_polling()

if __name__ == "__main__":
    main()