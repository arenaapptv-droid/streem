import asyncio, re, time, json, os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ========== تحميل الإعدادات ==========
with open("settings.json", "r") as f:
    settings = json.load(f)
    TOKEN = settings["TOKEN"]
    ADMIN_ID = settings["ADMIN_ID"]
    LOGO_URL_1 = settings["LOGO_URL_1"]
    LOGO_URL_2 = settings["LOGO_URL_2"]
    SLATE_IMAGE_URL = settings["SLATE_IMAGE_URL"]

CONFIG_FILE = "streams_config.json"
active_streams = {}       # {"stream_1": {...}, "stream_2": {...}, ...}
stream_locks = {}         # أقفال لكل بث لمنع التداخل
manual_stop_requested = set()  # مجموعة من معرفات البثوث المطلوب إيقافها يدويًا

# ========== إدارة ملف إعدادات البثوث ==========
def load_streams_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except: pass
    # إنشاء إعدادات افتراضية لـ 5 بثوث
    default = {}
    for i in range(1, 6):
        default[f"stream_{i}"] = {"server": "", "key": "", "source": ""}
    return default

def save_streams_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

streams_cfg = load_streams_config()

# ========== قراءة موارد الحاوية ==========
def get_container_stats(prev_usage, prev_time):
    cpu_percent = 0.0
    mem_used = 0
    mem_total = 0
    new_usage = 0
    new_time = time.time()

    try:
        with open("/sys/fs/cgroup/cpu/cpuacct.usage", "r") as f:
            usage_ns = int(f.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", "r") as f:
            period_us = int(f.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "r") as f:
            quota_us = int(f.read().strip())

        cores = max(1, quota_us / period_us) if period_us > 0 else 1
        if prev_usage and prev_time:
            delta_ns = usage_ns - prev_usage
            delta_sec = new_time - prev_time
            if delta_sec > 0:
                cpu_ratio = delta_ns / (delta_sec * 1e9)
                cpu_percent = cpu_ratio * 100 * cores
        new_usage = usage_ns
    except:
        cpu_percent = 0.0
        new_usage = 0

    try:
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r") as f:
            mem_used = int(f.read().strip()) // 1048576
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as f:
            mem_total = int(f.read().strip()) // 1048576
    except:
        try:
            with open("/sys/fs/cgroup/memory.current", "r") as f:
                mem_used = int(f.read().strip()) // 1048576
            with open("/sys/fs/cgroup/memory.max", "r") as f:
                mem_total = int(f.read().strip()) // 1048576
        except:
            mem_used = mem_total = 0

    return cpu_percent, mem_used, mem_total, new_usage, new_time

# ========== دوال البوت ==========
async def check_admin(update: Update) -> bool:
    if update.effective_user.id != ADMIN_ID:
        if update.message:
            await update.message.reply_text("🚫 هذا البوت مخصص للمالك فقط.")
        elif update.callback_query:
            await update.callback_query.answer("🚫 غير مصرح", show_alert=True)
        return False
    return True

def main_menu_keyboard():
    keyboard = []
    for i in range(1, 6):
        keyboard.append([InlineKeyboardButton(f"🎬 Stream {i}", callback_data=f"menu_stream_{i}")])
    return InlineKeyboardMarkup(keyboard)

def stream_control_keyboard(stream_id: str):
    """أزرار التحكم لبث واحد"""
    status = active_streams.get(stream_id)
    is_running = status is not None
    keyboard = [
        [InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{stream_id}")],
        [InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{stream_id}")],
        [InlineKeyboardButton("🔄 تغيير المصدر", callback_data=f"change_{stream_id}")],
        [InlineKeyboardButton("🟡 شاشة توقف", callback_data=f"slate_{stream_id}")],
        [InlineKeyboardButton("🏷 الشعار 1", callback_data=f"logo1_{stream_id}"),
         InlineKeyboardButton("🏷 الشعار 2", callback_data=f"logo2_{stream_id}")],
        [InlineKeyboardButton("⚙️ إعدادات السيرفر/المفتاح", callback_data=f"settings_{stream_id}")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
    ]
    if is_running:
        keyboard.insert(0, [InlineKeyboardButton("🟢 يعمل الآن", callback_data="noop")])
    return InlineKeyboardMarkup(keyboard)

async def stop_stream(stream_id: str, bot, manual=False):
    """إيقاف بث محدد"""
    global manual_stop_requested
    if stream_id in active_streams:
        if manual:
            manual_stop_requested.add(stream_id)
        stream = active_streams[stream_id]
        try:
            stream["process"].kill()
        except: pass
        try:
            await stream["process"].wait()
        except: pass
        try:
            await bot.edit_message_text(
                chat_id=ADMIN_ID,
                message_id=stream["frame_msg_id"],
                text=f"⏹ تم إيقاف البث {stream_id}"
            )
        except: pass
        del active_streams[stream_id]
        manual_stop_requested.discard(stream_id)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    await update.message.reply_text("🖥️ **Rplay Server – 5 Streams**", reply_markup=main_menu_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    text = update.message.text.strip()
    mode = context.user_data.get("mode")

    if mode and mode.startswith("source_"):
        stream_id = mode.split("_", 1)[1]
        context.user_data["mode"] = None
        streams_cfg[stream_id]["source"] = text
        save_streams_config(streams_cfg)
        await update.message.reply_text(f"✅ تم حفظ المصدر لـ {stream_id}")
        await start(update, context)
        return

    if mode and mode.startswith("server_"):
        stream_id = mode.split("_", 1)[1]
        context.user_data["mode"] = None
        streams_cfg[stream_id]["server"] = text
        save_streams_config(streams_cfg)
        await update.message.reply_text(f"✅ تم حفظ السيرفر لـ {stream_id}")
        await start(update, context)
        return

    if mode and mode.startswith("key_"):
        stream_id = mode.split("_", 1)[1]
        context.user_data["mode"] = None
        streams_cfg[stream_id]["key"] = text
        save_streams_config(streams_cfg)
        await update.message.reply_text(f"✅ تم حفظ المفتاح لـ {stream_id}")
        await start(update, context)
        return

async def run_stream(stream_id: str, context: ContextTypes.DEFAULT_TYPE, logo_url: str = None, is_slate: bool = False):
    global manual_stop_requested
    cfg = streams_cfg.get(stream_id)

    if not cfg or not cfg.get("server") or not cfg.get("key"):
        await context.bot.send_message(ADMIN_ID, f"❌ بيانات السيرفر/المفتاح غير موجودة لـ {stream_id}")
        return

    # منع تشغيل نفس البث مرتين
    if stream_id in active_streams and not is_slate:
        await context.bot.send_message(ADMIN_ID, f"❌ البث {stream_id} يعمل بالفعل")
        return

    output_url = f"{cfg['server']}/{cfg['key']}"
    input_url = cfg.get("source", "")

    # --- أمر FFmpeg ---
    if is_slate:
        cmd = [
            "ffmpeg",
            "-stream_loop", "-1",
            "-re",
            "-i", SLATE_IMAGE_URL,          # صورة ثابتة
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "stillimage",
            "-b:v", "500k",
            "-maxrate", "500k",
            "-bufsize", "1000k",
            "-c:a", "aac",
            "-b:a", "32k",
            "-ar", "44100",
            "-ac", "2",
            "-f", "flv", output_url
        ]
    else:
        cmd = [
            "ffmpeg",
            "-re",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-rw_timeout", "10000000",
            "-fflags", "+genpts+discardcorrupt",
            "-i", input_url,
            "-i", logo_url if logo_url else LOGO_URL_1,
            "-filter_complex",
            "[1:v][0:v] scale2ref=iw:ih [logo][ref]; [ref][logo] overlay=0:0",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-b:v", "9000k",
            "-maxrate", "9000k",
            "-bufsize", "18000k",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-f", "flv", output_url
        ]

    # إيقاف اي بث قديم بنفس المعرف
    await stop_stream(stream_id, context.bot)

    status_msg = await context.bot.send_message(ADMIN_ID, f"⏳ جاري تشغيل {stream_id}...")
    msg_id = status_msg.message_id

    while stream_id not in manual_stop_requested:
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"❌ فشل تشغيل {stream_id}: {e}")
            return

        # رسالة حالة
        if is_slate:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 استئناف البث", callback_data=f"resume_{stream_id}"),
                 InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{stream_id}")]
            ])
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"🟡 {stream_id} شاشة توقف", reply_markup=buttons)
        else:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("🟡 شاشة توقف", callback_data=f"slate_{stream_id}"),
                 InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{stream_id}")]
            ])
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"✅ {stream_id} يعمل", reply_markup=buttons)

        active_streams[stream_id] = {
            "process": process,
            "frame_msg_id": msg_id,
            "logo": logo_url if logo_url else LOGO_URL_1
        }

        last_update = time.time()
        try:
            while True:
                line = await process.stderr.readline()
                if not line: break
                decoded = line.decode("utf-8", errors="ignore").strip()

                if "fps=" in decoded:
                    now = time.time()
                    if now - last_update >= 5:
                        last_update = now
                        fps_match = re.search(r"fps=\s*([\d.]+)", decoded)
                        fps = fps_match.group(1) if fps_match else "0"
                        try:
                            await context.bot.edit_message_text(
                                chat_id=ADMIN_ID, message_id=msg_id,
                                text=f"{'🟡' if is_slate else '🟢'} {stream_id} | FPS: {fps}",
                                reply_markup=buttons
                            )
                        except: pass
                await asyncio.sleep(0.1)

            retcode = await process.wait()

            if stream_id in manual_stop_requested:
                await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"⏹ {stream_id} تم الإيقاف يدوياً")
                break

            await context.bot.edit_message_text(
                chat_id=ADMIN_ID, message_id=msg_id,
                text=f"⚠️ {stream_id} توقف (كود {retcode})، إعادة بعد 3 ثوانٍ..."
            )
            await asyncio.sleep(3)

        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"❌ {stream_id} خطأ: {e}")
            try: process.kill()
            except: pass
            break
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except: pass

    if stream_id in active_streams:
        del active_streams[stream_id]
    manual_stop_requested.discard(stream_id)

# ========== الأزرار التفاعلية ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global manual_stop_requested
    query = update.callback_query
    await query.answer()
    data = query.data

    if not await check_admin(update): return

    if data == "main_menu":
        await query.edit_message_text("🖥️ **Rplay Server – 5 Streams**", reply_markup=main_menu_keyboard())

    # --- الدخول إلى قائمة بث محدد ---
    elif data.startswith("menu_stream_"):
        stream_id = data.split("menu_stream_")[1]
        await query.edit_message_text(f"🎛️ التحكم في {stream_id}", reply_markup=stream_control_keyboard(stream_id))

    # --- تشغيل بث (يطلب المصدر إذا لم يكن محفوظاً) ---
    elif data.startswith("start_"):
        stream_id = data.split("start_")[1]
        cfg = streams_cfg.get(stream_id)
        if not cfg or not cfg.get("server") or not cfg.get("key"):
            await query.edit_message_text(f"❌ تحتاج إلى ضبط السيرفر والمفتاح لـ {stream_id} أولاً.")
            return
        if not cfg.get("source"):
            context.user_data["mode"] = f"source_{stream_id}"
            await query.edit_message_text(f"📥 أرسل رابط المصدر لـ {stream_id}:")
        else:
            await query.edit_message_text(f"⏳ جاري تشغيل {stream_id}...")
            asyncio.create_task(run_stream(stream_id, context))

    # --- إيقاف بث ---
    elif data.startswith("stop_"):
        stream_id = data.split("stop_")[1]
        await stop_stream(stream_id, context.bot, manual=True)
        await query.edit_message_text(f"⏹ {stream_id} تم الإيقاف")

    # --- تغيير المصدر ---
    elif data.startswith("change_"):
        stream_id = data.split("change_")[1]
        context.user_data["mode"] = f"source_{stream_id}"
        await query.edit_message_text(f"📥 أرسل رابط المصدر الجديد لـ {stream_id}:")

    # --- شاشة توقف ---
    elif data.startswith("slate_"):
        stream_id = data.split("slate_")[1]
        await query.edit_message_text(f"🟡 جاري تشغيل شاشة التوقف لـ {stream_id}...")
        asyncio.create_task(run_stream(stream_id, context, is_slate=True))

    # --- استئناف بث من الشاشة ---
    elif data.startswith("resume_"):
        stream_id = data.split("resume_")[1]
        await query.edit_message_text(f"🔙 جاري استئناف {stream_id}...")
        asyncio.create_task(run_stream(stream_id, context))

    # --- اختيار شعار 1 ---
    elif data.startswith("logo1_"):
        stream_id = data.split("logo1_")[1]
        await query.edit_message_text(f"🏷 جاري تشغيل {stream_id} بالشعار 1...")
        asyncio.create_task(run_stream(stream_id, context, logo_url=LOGO_URL_1))

    # --- اختيار شعار 2 ---
    elif data.startswith("logo2_"):
        stream_id = data.split("logo2_")[1]
        await query.edit_message_text(f"🏷 جاري تشغيل {stream_id} بالشعار 2...")
        asyncio.create_task(run_stream(stream_id, context, logo_url=LOGO_URL_2))

    # --- إعدادات السيرفر/المفتاح ---
    elif data.startswith("settings_"):
        stream_id = data.split("settings_")[1]
        cfg = streams_cfg.get(stream_id, {})
        server = cfg.get("server", "غير محدد")
        key = cfg.get("key", "غير محدد")
        await query.edit_message_text(
            f"⚙️ إعدادات {stream_id}\n🔗 السيرفر: {server}\n🔑 المفتاح: {key}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("تعديل السيرفر", callback_data=f"setsrv_{stream_id}")],
                [InlineKeyboardButton("تعديل المفتاح", callback_data=f"setkey_{stream_id}")],
                [InlineKeyboardButton("🔙 رجوع", callback_data=f"menu_stream_{stream_id}")],
            ])
        )

    elif data.startswith("setsrv_"):
        stream_id = data.split("setsrv_")[1]
        context.user_data["mode"] = f"server_{stream_id}"
        await query.edit_message_text(f"🔗 أرسل رابط السيرفر الجديد لـ {stream_id}:")

    elif data.startswith("setkey_"):
        stream_id = data.split("setkey_")[1]
        context.user_data["mode"] = f"key_{stream_id}"
        await query.edit_message_text(f"🔑 أرسل المفتاح الجديد لـ {stream_id}:")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Rplay Server – 5 Streams يعمل...")
    app.run_polling()
