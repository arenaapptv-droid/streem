import asyncio
import json
import os
import re
import shutil
import time
import aiohttp
from collections import defaultdict

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ========== الإعدادات ==========
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
    for sid, s in streams.items():
        if "viewers" in s and isinstance(s["viewers"], list):
            s["viewers"] = set(s["viewers"])
        s.setdefault("fps", "?")
        s.setdefault("logo", "")
        s.setdefault("ua", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        s.setdefault("rtmp_server", "")
        s.setdefault("rtmp_key", "")
        s.setdefault("type", "hls")
        s.setdefault("mode", "copy")
        s.setdefault("chat_id", None)
        s.setdefault("msg_id", None)
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
        return f"CPU: {cpu}%\nRAM: {mem.percent}%\nDISK: {disk.percent}%\nStreams: {len(streams)}"
    except:
        return f"Streams: {len(streams)}"

# ========== مراقبة السيرفر ==========
monitor_active = False
monitor_task = None

async def monitor_loop(bot, chat_id, msg_id):
    global monitor_active
    while monitor_active:
        text = system_status()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف", callback_data="stop_monitor")]])
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
        except:
            pass
        await asyncio.sleep(2)

# ========== واجهة البوت ==========
reply_kb = ReplyKeyboardMarkup([
    ["📺 HLS", "📡 RTMP"],
    ["➕ إضافة", "🖥 مراقبة"],
    ["🧹 تنظيف"]
], resize_keyboard=True)

def streams_list(typ):
    kb = []
    for sid, s in streams.items():
        if s["type"] == typ:
            status = "🟢" if s.get("active") else "🔴"
            kb.append([InlineKeyboardButton(f"{status} {s['name']}", callback_data=f"open_{sid}")])
    kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")])
    return InlineKeyboardMarkup(kb)

def panel_keyboard(sid, s):
    active = s.get("active")
    mode = s.get("mode")
    kb = []
    if active:
        kb.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{sid}")])
    else:
        kb.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{sid}")])
    kb.append([InlineKeyboardButton("📥 مصدر", callback_data=f"src_{sid}"), InlineKeyboardButton("🖼 شعار", callback_data=f"logo_{sid}")])
    kb.append([InlineKeyboardButton("🕵️ UA", callback_data=f"ua_{sid}"), InlineKeyboardButton("✏️ اسم", callback_data=f"rename_{sid}")])
    if s["type"] == "rtmp":
        kb.append([InlineKeyboardButton("📡 سيرفر", callback_data=f"srv_{sid}"), InlineKeyboardButton("🔑 مفتاح", callback_data=f"key_{sid}")])
    toggle = "🔄 نسخ" if mode == "encode" else "⚙️ ترميز"
    kb.append([InlineKeyboardButton(toggle, callback_data=f"mode_{sid}")])
    kb.append([InlineKeyboardButton("🗑 حذف", callback_data=f"del_{sid}")])
    if active:
        viewers_count = len(viewers.get(sid, set()))
        uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
        kb.append([InlineKeyboardButton(f"FPS:{s.get('fps','?')} | 👥{viewers_count} | ⏱️{uptime}", callback_data="noop")])
    kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")])
    return InlineKeyboardMarkup(kb)

async def update_panel(sid, bot):
    s = streams.get(sid)
    if not s or not s.get("chat_id") or not s.get("msg_id"):
        return
    text = f"🎛️ {s['name']}\n📥 {s['source']}\n🖼 {'✅' if s.get('logo') else '❌'}\n🕵️ {s.get('ua')}\n⚙️ {'نسخ' if s['mode']=='copy' else 'ترميز'}\n🟢 {'يعمل' if s.get('active') else 'متوقف'}\n🎬 FPS: {s.get('fps','?')}\n👥 {len(viewers.get(sid, set()))}\n⏱️ {time.strftime('%H:%M:%S', time.gmtime(time.time()-s['start_time'])) if s.get('start_time') else '00:00:00'}"
    if s["type"] == "hls":
        text += f"\n🔗 {BASE_URL}:{PORT}/live/{sid}/index.m3u8"
    else:
        text += f"\n📡 {s.get('rtmp_server')}/{s.get('rtmp_key')}"
    try:
        await bot.edit_message_text(text, s["chat_id"], s["msg_id"], reply_markup=panel_keyboard(sid, s))
    except: pass

# ========== دالة للتحقق من صحة الرابط ==========
async def check_url(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10, allow_redirects=True) as resp:
                return resp.status == 200
    except:
        return False

# ========== تشغيل البث (مع تحسينات قوية) ==========
async def start_stream(sid, bot):
    s = streams[sid]
    src = s["source"]
    mode = s["mode"]
    ua = s.get("ua", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    logo = s.get("logo", "")
    typ = s["type"]

    # التحقق من صحة رابط المصدر قبل البدء
    if typ == "hls":
        if not await check_url(src):
            await bot.send_message(s["chat_id"], f"❌ فشل الاتصال بالمصدر: {src}\nالرابط لا يعمل أو لا يمكن الوصول إليه.")
            return

    # التحقق من صحة إعدادات RTMP
    if typ == "rtmp":
        if not s.get("rtmp_server") or not s.get("rtmp_key"):
            await bot.send_message(s["chat_id"], "❌ إعدادات RTMP غير مكتملة (خادم أو مفتاح).")
            return

    out_dir = os.path.join(HLS_DIR, sid)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "index.m3u8")

    # إعدادات ffmpeg الأساسية والقوية (مقاومة للأخطاء)
    base_cmd = [
        "ffmpeg", "-re",
        "-i", src,
        "-user_agent", ua,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-timeout", "20000000",
        "-rw_timeout", "20000000",
        "-analyzeduration", "10000000",
        "-probesize", "100000000",
        "-fflags", "nobuffer+genpts",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto"
    ]

    if mode == "copy":
        cmd = base_cmd + ["-c:v", "copy", "-c:a", "copy"]
    else:
        video_opts = [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
            "-g", "90", "-vsync", "cfr", "-r", "30"
        ]
        if logo and len(logo) > 5:
            # يجب التأكد من صحة رابط الشعار أيضاً
            if not await check_url(logo):
                logo = ""  # تجاهل الشعار إذا كان الرابط لا يعمل
            if logo:
                video_opts = ["-i", logo, "-filter_complex", "[1:v][0:v]scale2ref=iw:ih[logo][ref];[ref][logo]overlay=0:0"] + video_opts
            else:
                video_opts = ["-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease"] + video_opts
        else:
            video_opts = ["-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease"] + video_opts
        cmd = base_cmd + video_opts + ["-c:a", "aac", "-b:a", "128k"]

    if typ == "hls":
        cmd += ["-f", "hls", "-hls_time", "2", "-hls_list_size", "5", "-hls_flags", "delete_segments", "-y", out_file]
    else:
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        cmd += ["-f", "flv", "-y", rtmp_url]

    if sid in processes:
        try:
            processes[sid].terminate()
            await asyncio.sleep(0.5)
            processes[sid].kill()
        except: pass

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
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
            # طباعة الأخطاء المهمة فقط
            if "error" in txt.lower() and not "deprecated pixel format" in txt.lower():
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
async def callback(update, context):
    global monitor_active, monitor_task
    q = update.callback_query
    try: await q.answer()
    except: pass
    data = q.data
    chat_id = q.message.chat_id
    msg_id = q.message.message_id

    if data == "back_to_main":
        await q.message.delete()
        await context.bot.send_message(chat_id, "القائمة الرئيسية", reply_markup=reply_kb)
        return

    if data == "stop_monitor":
        if monitor_active:
            monitor_active = False
            if monitor_task: monitor_task.cancel()
        await q.edit_message_text(system_status())
        await context.bot.send_message(chat_id, "القائمة الرئيسية", reply_markup=reply_kb)
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
            streams[sid]["msg_id"] = msg_id
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
                await q.answer("لا يوجد مصدر!", True)
                return
            if s.get("active"):
                await q.answer("البث يعمل بالفعل", True)
                return
            await q.answer("⏳ جاري التشغيل...")
            asyncio.create_task(start_stream(sid, context.bot))
        return

    if data.startswith("stop_"):
        sid = data[5:]
        await q.answer("⏹ تم الإيقاف")
        asyncio.create_task(stop_stream(sid, context.bot))
        return

    if data.startswith("src_"):
        sid = data[4:]
        context.user_data["edit"] = ("source", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل رابط المصدر الجديد:")
        return
    if data.startswith("logo_"):
        sid = data[5:]
        context.user_data["edit"] = ("logo", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل رابط الشعار (أو /skip):")
        return
    if data.startswith("ua_"):
        sid = data[3:]
        context.user_data["edit"] = ("ua", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل User-Agent (أو /skip):")
        return
    if data.startswith("rename_"):
        sid = data[7:]
        context.user_data["edit"] = ("name", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل الاسم الجديد:")
        return
    if data.startswith("srv_"):
        sid = data[4:]
        context.user_data["edit"] = ("rtmp_server", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل خادم RTMP:")
        return
    if data.startswith("key_"):
        sid = data[4:]
        context.user_data["edit"] = ("rtmp_key", sid, chat_id, msg_id)
        await q.edit_message_text("أرسل مفتاح RTMP:")
        return

    if data.startswith("mode_"):
        sid = data[5:]
        if sid in streams:
            old = streams[sid]["mode"]
            new = "encode" if old == "copy" else "copy"
            streams[sid]["mode"] = new
            save()
            await q.answer(f"✅ الوضع: {'ترميز' if new=='encode' else 'نسخ'}")
            if streams[sid].get("active"):
                await stop_stream(sid, context.bot)
                asyncio.create_task(start_stream(sid, context.bot))
            else:
                await update_panel(sid, context.bot)
        return

    if data.startswith("del_"):
        sid = data[4:]
        await stop_stream(sid, context.bot)
        if sid in streams:
            del streams[sid]
            save()
        await q.edit_message_text("🗑 تم الحذف")
        await context.bot.send_message(chat_id, "القائمة الرئيسية", reply_markup=reply_kb)
        return

async def handle_text(update, context):
    global monitor_active, monitor_task
    if update.effective_user.id != ADMIN_ID: return
    text = update.message.text
    chat_id = update.message.chat_id

    if text == "📺 HLS":
        await update.message.reply_text("📺 قائمة HLS:", reply_markup=streams_list("hls"))
        return
    if text == "📡 RTMP":
        await update.message.reply_text("📡 قائمة RTMP:", reply_markup=streams_list("rtmp"))
        return
    if text == "➕ إضافة":
        context.user_data["step"] = "add_name"
        await update.message.reply_text("📝 أرسل اسم البث:")
        return
    if text == "🖥 مراقبة":
        if monitor_active:
            monitor_active = False
            if monitor_task: monitor_task.cancel()
            await update.message.reply_text(system_status(), reply_markup=reply_kb)
            return
        status = system_status()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف", callback_data="stop_monitor")]])
        msg = await update.message.reply_text(status, reply_markup=kb)
        monitor_active = True
        monitor_task = asyncio.create_task(monitor_loop(context.bot, msg.chat_id, msg.message_id))
        return
    if text == "🧹 تنظيف":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        await update.message.reply_text("✅ تم التنظيف")
        return

    # إضافة بث جديد
    if context.user_data.get("step") == "add_name":
        name = text.strip()
        sid = name.replace(" ", "_")
        if sid in streams:
            c = 1
            while f"{sid}_{c}" in streams: c+=1
            sid = f"{sid}_{c}"
        streams[sid] = {"name": name, "source": "", "type": "hls", "mode": "copy", "active": False, "fps": "?", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "logo": "", "rtmp_server": "", "rtmp_key": "", "chat_id": None, "msg_id": None, "start_time": 0}
        save()
        context.user_data["step"] = "add_source"
        context.user_data["sid"] = sid
        await update.message.reply_text("📥 أرسل رابط المصدر:")
        return
    if context.user_data.get("step") == "add_source":
        sid = context.user_data.get("sid")
        if sid in streams:
            # التحقق من صحة الرابط قبل الحفظ
            if not await check_url(text):
                await update.message.reply_text("⚠️ الرابط الذي أدخلته قد لا يعمل. هل تريد المتابعة؟ (أرسل 'نعم' للمتابعة أو أي شيء آخر للإلغاء)")
                context.user_data["waiting_for_confirm"] = sid
                context.user_data["pending_url"] = text
                return
            streams[sid]["source"] = text
            save()
            context.user_data.pop("step")
            context.user_data.pop("sid")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("HLS", callback_data=f"settype_{sid}_hls"), InlineKeyboardButton("RTMP", callback_data=f"settype_{sid}_rtmp")]])
            await update.message.reply_text("اختر النوع:", reply_markup=kb)
        else:
            await update.message.reply_text("❌ خطأ")
        return

    if context.user_data.get("waiting_for_confirm"):
        sid = context.user_data["waiting_for_confirm"]
        if text == "نعم":
            streams[sid]["source"] = context.user_data["pending_url"]
            save()
            context.user_data.pop("waiting_for_confirm")
            context.user_data.pop("pending_url")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("HLS", callback_data=f"settype_{sid}_hls"), InlineKeyboardButton("RTMP", callback_data=f"settype_{sid}_rtmp")]])
            await update.message.reply_text("تم حفظ الرابط رغم التحذير. اختر النوع:", reply_markup=kb)
        else:
            context.user_data.pop("waiting_for_confirm")
            context.user_data.pop("pending_url")
            await update.message.reply_text("تم إلغاء الإضافة. استخدم '➕ إضافة' مرة أخرى.")
        return

    # تعديل بيانات البث
    if context.user_data.get("edit"):
        typ, sid, edit_chat, edit_msg = context.user_data["edit"]
        s = streams.get(sid)
        if s:
            if typ == "source":
                # التحقق من صحة الرابط الجديد
                if not await check_url(text):
                    await update.message.reply_text("⚠️ الرابط الجديد قد لا يعمل. هل تريد المتابعة؟ (أرسل 'نعم' للمتابعة)")
                    context.user_data["waiting_confirm_edit"] = (typ, sid, text, edit_chat, edit_msg)
                    return
                s["source"] = text
            elif typ == "logo": s["logo"] = "" if text == "/skip" else text
            elif typ == "ua": s["ua"] = text if text != "/skip" else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            elif typ == "name": s["name"] = text
            elif typ == "rtmp_server": s["rtmp_server"] = text
            elif typ == "rtmp_key": s["rtmp_key"] = text
            save()
            context.user_data.pop("edit")
            await update_panel(sid, context.bot)
            await update.message.delete()
        return

    if context.user_data.get("waiting_confirm_edit"):
        typ, sid, url, edit_chat, edit_msg = context.user_data["waiting_confirm_edit"]
        if text == "نعم":
            streams[sid]["source"] = url
            save()
            await update.message.reply_text("تم تحديث المصدر رغم التحذير.")
            await update_panel(sid, context.bot)
            await update.message.delete()
        else:
            await update.message.reply_text("تم إلغاء التحديث.")
        context.user_data.pop("waiting_confirm_edit")
        return

async def set_type(update, context):
    q = update.callback_query
    try: await q.answer()
    except: pass
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

async def start(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("غير مصرح")
        return
    await update.message.reply_text("🎬 بوت البث المباشر\nاستخدم الأزرار السفلية:", reply_markup=reply_kb)

# ========== التشغيل ==========
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_http())
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(callback, pattern="^(?!settype_).*"))
    app.add_handler(CallbackQueryHandler(set_type, pattern="^settype_"))
    print("🚀 البوت يعمل...")
    app.run_polling()

if __name__ == "__main__":
    main()