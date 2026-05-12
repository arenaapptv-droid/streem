import asyncio, re, time, json, os, subprocess
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ==================== تحميل الإعدادات ====================
with open("settings.json", "r") as f:
    settings = json.load(f)
    TOKEN = settings["TOKEN"]
    ADMIN_ID = settings["ADMIN_ID"]
    SLATE_IMAGE = settings.get("SLATE_IMAGE_URL", "https://i.postimg.cc/GmnKtYtL/20260508-163600.png")

STREAMS_FILE = "streams_config.json"

# بنية البثوث النشطة
active = {}          # {stream_id: {"proc": process, "msg": int, "stop": threading.Event}}
pending_source = {}  # {stream_id: str}   # رابط المصدر المؤقت

def load_config():
    if os.path.exists(STREAMS_FILE):
        try:
            with open(STREAMS_FILE) as f:
                return json.load(f)
        except: pass
    return {f"stream_{i}": {"server": "", "key": ""} for i in range(1, 10)}

def save_config(cfg):
    with open(STREAMS_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

streams_cfg = load_config()

# ==================== موارد السيرفر ====================
def cpu_usage():
    try:
        with open("/proc/stat") as f:
            p = f.readline().split()
            if p[0] != "cpu": return 0.0
            u1 = sum(map(int, p[1:5]))
            id1 = int(p[4])
        time.sleep(0.1)
        with open("/proc/stat") as f:
            p = f.readline().split()
            u2 = sum(map(int, p[1:5]))
            id2 = int(p[4])
        total = u2 - u1
        return 0.0 if total == 0 else round(100.0 * (1 - (id2 - id1) / total), 1)
    except: return 0.0

def ram_usage():
    try:
        with open("/proc/meminfo") as f:
            lines = f.read()
            total = int(re.search(r"MemTotal:\s+(\d+)", lines).group(1))
            avail = int(re.search(r"MemAvailable:\s+(\d+)", lines).group(1))
            return (total - avail) // 1024, total // 1024
    except: return 0, 0

# ==================== الأدوات ====================
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
        if i % 2 == 1:
            row = [InlineKeyboardButton(f"🎬 Stream {i}", callback_data=f"menu_stream_{i}")]
        else:
            row.append(InlineKeyboardButton(f"🎬 Stream {i}", callback_data=f"menu_stream_{i}"))
            kb.append(row)
    if 9 % 2 == 1: kb.append(row)
    kb.append([InlineKeyboardButton("🖥 حالة السيرفر", callback_data="status")])
    return InlineKeyboardMarkup(kb)

def control_menu(sid):
    running = sid in active
    kb = []
    if running:
        kb.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{sid}")])
    else:
        kb.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{sid}")])
    kb.append([InlineKeyboardButton("🔄 تغيير المصدر", callback_data=f"change_{sid}")])
    kb.append([InlineKeyboardButton("🟡 شاشة توقف", callback_data=f"slate_{sid}")])
    kb.append([InlineKeyboardButton("⚙️ إعدادات", callback_data=f"cfg_{sid}")])
    kb.append([InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

# ==================== إيقاف بث ====================
async def stop_stream(sid, bot):
    if sid in active:
        active[sid]["stop"].set()          # إشارة للتوقف
        try:
            active[sid]["proc"].kill()
        except: pass
        try:
            await active[sid]["proc"].wait()
        except: pass
        msg_id = active[sid]["msg"]
        try:
            await bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id,
                                        text=f"⏹ Stream {sid.split('_')[1]} متوقف")
        except: pass
        del active[sid]

# ==================== تشغيل البث ====================
async def stream_runner(sid, context, is_slate=False):
    cfg = streams_cfg.get(sid)
    if not cfg or not cfg.get("server") or not cfg.get("key"):
        await context.bot.send_message(ADMIN_ID, f"❌ {sid}: السيرفر أو المفتاح مفقود")
        return
    src = pending_source.get(sid)
    if not is_slate and not src:
        await context.bot.send_message(ADMIN_ID, f"❌ {sid}: المصدر غير محدد")
        return
    if sid in active:
        await context.bot.send_message(ADMIN_ID, f"❌ {sid} يعمل بالفعل")
        return

    out = f"{cfg['server']}/{cfg['key']}"
    name = f"Stream {sid.split('_')[1]}"

    # إعدادات FFmpeg مع User‑Agent ExoPlayer
    if is_slate:
        cmd = [
            "ffmpeg", "-re", "-loop", "1", "-i", SLATE_IMAGE,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
            "-b:v", "500k", "-maxrate", "500k", "-bufsize", "1000k",
            "-c:a", "aac", "-b:a", "32k", "-ar", "44100", "-ac", "2",
            "-f", "flv", out
        ]
    else:
        cmd = [
            "ffmpeg", "-re",
            "-user_agent", "Mozilla/5.0 (Linux; Android 13) ExoPlayerLib/2.18.5",
            "-i", src,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-flvflags", "no_duration_filesize",
            "-rtmp_live", "live",
            "-f", "flv", out
        ]

    msg = await context.bot.send_message(ADMIN_ID, f"⏳ {name} يبدأ...")
    mid = msg.message_id
    stop_ev = threading.Event()
    active[sid] = {"proc": None, "msg": mid, "stop": stop_ev}

    retries = 0
    while retries < 10 and not stop_ev.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            active[sid]["proc"] = proc
        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid,
                                                text=f"❌ فشل تشغيل {name}: {e}")
            break

        # انتظار قصير لبدء البث
        await asyncio.sleep(2)
        if proc.returncode is not None and not stop_ev.is_set():
            retries += 1
            delay = min(10 * retries, 60)
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid,
                text=f"⚠️ {name} فشل (كود {proc.returncode}) - المحاولة {retries}/10 بعد {delay}s")
            await asyncio.sleep(delay)
            continue

        # تحديث واجهة "يعمل"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{sid}"),
             InlineKeyboardButton("🔄 تغيير", callback_data=f"change_{sid}")]
        ])
        await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid,
                                            text=f"🟢 {name} يعمل", reply_markup=kb)

        last_upd = time.time()
        try:
            while proc.returncode is None and not stop_ev.is_set():
                line = await proc.stderr.readline()
                if line:
                    dec = line.decode(errors="ignore").strip()
                    if "fps=" in dec and time.time() - last_upd >= 5:
                        last_upd = time.time()
                        fps = re.search(r"fps=\s*([\d.]+)", dec)
                        fps = fps.group(1) if fps else "?"
                        tm = re.search(r"time=(\d+:\d+:\d+\.\d+)", dec)
                        tm = tm.group(1) if tm else "00:00"
                        sp = re.search(r"speed=\s*([\d.]+)x", dec)
                        sp = sp.group(1) if sp else "?"
                        txt = f"🟢 {name}\n📊 FPS:{fps} ⏱{tm} ⚡{sp}x"
                        try:
                            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid, text=txt, reply_markup=kb)
                        except: pass
                await asyncio.sleep(0.1)
        except: pass

        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if stop_ev.is_set():
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid, text=f"⏹ {name} متوقف")
            break
        retries += 1
        delay = min(10 * retries, 60)
        await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid,
            text=f"⚠️ {name} توقف - إعادة {retries}/10 بعد {delay}s")
        await asyncio.sleep(delay)

    if sid in active:
        del active[sid]
    if sid in pending_source:
        del pending_source[sid]

# ==================== الأزرار ====================
async def button_handler(update, context):
    q = update.callback_query
    await q.answer()
    d = q.data
    if not await check_admin(update): return

    if d == "status":
        cpu = cpu_usage()
        ru, rt = ram_usage()
        await q.edit_message_text(f"🖥 CPU:{cpu}% | RAM:{ru}/{rt}MiB", reply_markup=main_menu())
    elif d == "main_menu":
        await q.edit_message_text("🖥 **Rplay Streaming Panel**", reply_markup=main_menu())

    elif "_" in d:
        act, sid = d.split("_", 1)
        if act == "menu":
            await q.edit_message_text(f"🎛 {sid}", reply_markup=control_menu(sid))
        elif act == "start":
            cfg = streams_cfg.get(sid)
            if not cfg or not cfg.get("server") or not cfg.get("key"):
                await q.edit_message_text("❌ يرجى إعداد السيرفر والمفتاح أولاً")
                return
            context.user_data["mode"] = f"source_{sid}"
            await q.edit_message_text(f"📥 أرسل رابط المصدر للبث {sid.split('_')[1]}:")
        elif act == "stop":
            await stop_stream(sid, context.bot)
            await q.edit_message_text(f"⏹ {sid} تم إيقافه")
        elif act == "change":
            context.user_data["mode"] = f"source_{sid}"
            await q.edit_message_text(f"📥 أرسل المصدر الجديد لـ {sid}:")
        elif act == "slate":
            pending_source[sid] = SLATE_IMAGE
            await q.edit_message_text(f"🟡 شاشة توقف {sid.split('_')[1]}...")
            asyncio.create_task(stream_runner(sid, context, is_slate=True))
        elif act == "cfg":
            cfg = streams_cfg.get(sid, {})
            await q.edit_message_text(
                f"⚙️ {sid}\n🔗 {cfg.get('server','?')}\n🔑 {cfg.get('key','?')}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("تعديل السيرفر", callback_data=f"srv_{sid}"),
                     InlineKeyboardButton("تعديل المفتاح", callback_data=f"key_{sid}")],
                    [InlineKeyboardButton("🔙", callback_data=f"menu_{sid}")]]))
        elif act == "srv":
            context.user_data["mode"] = f"server_{sid}"
            await q.edit_message_text(f"🔗 أرسل السيرفر لـ {sid}:")
        elif act == "key":
            context.user_data["mode"] = f"key_{sid}"
            await q.edit_message_text(f"🔑 أرسل المفتاح لـ {sid}:")
        elif act == "delete":
            streams_cfg[sid] = {"server": "", "key": ""}
            save_config(streams_cfg)
            await q.edit_message_text(f"🗑 تم مسح إعدادات {sid}")

# ==================== استقبال النصوص (روابط) ====================
async def msg_handler(update, context):
    if not await check_admin(update): return
    text = update.message.text.strip()
    mode = context.user_data.get("mode")
    if mode and "_" in mode:
        act, sid = mode.split("_", 1)
        if act == "source" and sid:
            context.user_data["mode"] = None
            pending_source[sid] = text
            await update.message.reply_text(f"⏳ تشغيل {sid}...")
            asyncio.create_task(stream_runner(sid, context))
        elif act == "server" and sid:
            context.user_data["mode"] = None
            streams_cfg[sid]["server"] = text
            save_config(streams_cfg)
            await update.message.reply_text("✅ تم حفظ السيرفر")
        elif act == "key" and sid:
            context.user_data["mode"] = None
            streams_cfg[sid]["key"] = text
            save_config(streams_cfg)
            await update.message.reply_text("✅ تم حفظ المفتاح")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    print("✅ لوحة البث تعمل...")
    app.run_polling()