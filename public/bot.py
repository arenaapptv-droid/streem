import asyncio, time, json, os, subprocess
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
active_streams = {}        # {stream_id: {'process': ..., 'frame_msg_id': ..., 'manual_stop': bool, 'input_url': ...}}
stream_tasks = {}          # {stream_id: asyncio.Task}
stream_locks = {}          # {stream_id: asyncio.Lock}
manual_stop_flags = {}     # {stream_id: bool}

# ========== إدارة ملف الإعدادات ==========
def load_streams_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except: pass
    # إعدادات افتراضية لـ 9 بثوث
    default = {}
    for i in range(1, 10):
        default[f"stream_{i}"] = {"server": "", "key": "", "source": ""}
    return default

def save_streams_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

streams_cfg = load_streams_config()

# ========== دوال حالة السيرفر ==========
def get_cpu_usage():
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()
            parts = line.split()
            if parts[0] != "cpu": return 0.0
            user, nice, system, idle = map(int, parts[1:5])
            total1 = user + nice + system + idle
            idle1 = idle
        time.sleep(0.1)
        with open("/proc/stat", "r") as f:
            line = f.readline()
            parts = line.split()
            if parts[0] != "cpu": return 0.0
            user, nice, system, idle = map(int, parts[1:5])
            total2 = user + nice + system + idle
            idle2 = idle
        delta_total = total2 - total1
        delta_idle = idle2 - idle1
        if delta_total == 0: return 0.0
        return 100.0 * (1.0 - delta_idle / delta_total)
    except:
        return 0.0

def get_ram_usage():
    try:
        with open("/proc/meminfo", "r") as f:
            memtotal = 0
            memavailable = 0
            for line in f:
                if line.startswith("MemTotal:"):
                    memtotal = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    memavailable = int(line.split()[1])
            used = (memtotal - memavailable) // 1024
            total = memtotal // 1024
            return used, total
    except:
        return 0, 0

def get_disk_usage():
    try:
        result = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 4:
                return parts[2], parts[1]
    except:
        pass
    return "N/A", "N/A"

# ========== دوال مساعدة ==========
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
    # صفين من 5 أزرار لكل صف
    for i in range(1, 10):
        if i % 2 == 1:
            row = [InlineKeyboardButton(f"🎬 Stream {i}", callback_data=f"menu_stream_{i}")]
        else:
            row.append(InlineKeyboardButton(f"🎬 Stream {i}", callback_data=f"menu_stream_{i}"))
            keyboard.append(row)
    if 9 % 2 == 1:  # إذا كان العدد فردي، نضيف الصف الأخير
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🖥️ مراقبة السيرفر", callback_data="server_status")])
    return InlineKeyboardMarkup(keyboard)

def stream_control_keyboard(stream_id: str):
    """أزرار التحكم لبث واحد"""
    status = active_streams.get(stream_id)
    is_running = status is not None
    keyboard = []
    if not is_running:
        keyboard.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{stream_id}")])
    else:
        keyboard.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{stream_id}")])
    keyboard.append([InlineKeyboardButton("🔄 تغيير المصدر", callback_data=f"change_{stream_id}")])
    keyboard.append([InlineKeyboardButton("🟡 شاشة توقف", callback_data=f"slate_{stream_id}")])
    keyboard.append([InlineKeyboardButton("⚙️ إعدادات السيرفر/المفتاح", callback_data=f"settings_{stream_id}")])
    keyboard.append([InlineKeyboardButton("🗑 حذف الإعدادات", callback_data=f"delete_{stream_id}")])
    keyboard.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

async def stop_stream(stream_id: str, bot, manual=False):
    global manual_stop_flags
    if stream_id in active_streams:
        if manual:
            manual_stop_flags[stream_id] = True
        stream = active_streams[stream_id]
        try:
            stream["process"].kill()
        except: pass
        try:
            await stream["process"].wait()
        except: pass
        try:
            if stream.get("frame_msg_id"):
                await bot.edit_message_text(
                    chat_id=ADMIN_ID,
                    message_id=stream["frame_msg_id"],
                    text=f"⏹ تم إيقاف البث {stream_id}"
                )
        except: pass
        del active_streams[stream_id]
        manual_stop_flags.pop(stream_id, None)

    # إلغاء المهمة إن وجدت
    if stream_id in stream_tasks:
        task = stream_tasks[stream_id]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        del stream_tasks[stream_id]

async def update_server_status(query, chat_id, message_id):
    try:
        while True:
            cpu = get_cpu_usage()
            ram_used, ram_total = get_ram_usage()
            disk_used, disk_total = get_disk_usage()
            text = (
                f"🖥️ **حالة السيرفر (مباشر)**\n"
                f"CPU: {cpu:.1f}%\n"
                f"RAM: {ram_used} MiB / {ram_total} MiB\n"
                f"Disk: {disk_used} / {disk_total}"
            )
            try:
                await query.edit_message_text(
                    text, parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 تحديث الآن", callback_data="server_status"),
                         InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")]
                    ])
                )
            except:
                pass
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass

# ========== أوامر البوت ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    await update.message.reply_text("🖥️ **Rplay Server – 9 Streams**", reply_markup=main_menu_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    text = update.message.text.strip()
    mode = context.user_data.get("mode")

    if mode and "_" in mode:
        parts = mode.split("_", 1)
        action = parts[0]
        stream_id = parts[1] if len(parts) > 1 else ""

        if action == "source" and stream_id:
            # استلام مصدر لبث معين
            context.user_data["mode"] = None
            streams_cfg[stream_id]["source"] = text
            save_streams_config(streams_cfg)
            await update.message.reply_text(f"✅ تم حفظ المصدر لـ {stream_id}")
            # إذا كان هناك طلب تشغيل معلق، نبدأ البث
            if context.user_data.get("start_after_source"):
                context.user_data["start_after_source"] = False
                asyncio.create_task(run_stream(stream_id, context))
            return

        elif action == "server" and stream_id:
            context.user_data["mode"] = None
            streams_cfg[stream_id]["server"] = text
            save_streams_config(streams_cfg)
            await update.message.reply_text(f"✅ تم حفظ السيرفر لـ {stream_id}")
            return

        elif action == "key" and stream_id:
            context.user_data["mode"] = None
            streams_cfg[stream_id]["key"] = text
            save_streams_config(streams_cfg)
            await update.message.reply_text(f"✅ تم حفظ المفتاح لـ {stream_id}")
            return

async def run_stream(stream_id: str, context: ContextTypes.DEFAULT_TYPE, is_slate: bool = False):
    global active_streams, manual_stop_flags
    cfg = streams_cfg.get(stream_id)

    if not cfg or not cfg.get("server") or not cfg.get("key"):
        await context.bot.send_message(ADMIN_ID, f"❌ بيانات السيرفر/المفتاح غير موجودة لـ {stream_id}")
        return

    if not is_slate and (not cfg.get("source")):
        await context.bot.send_message(ADMIN_ID, f"❌ المصدر غير محدد لـ {stream_id}")
        return

    # منع تشغيل نفس البث مرتين
    if stream_id in active_streams:
        await context.bot.send_message(ADMIN_ID, f"❌ البث {stream_id} يعمل بالفعل")
        return

    output_url = f"{cfg['server']}/{cfg['key']}"
    input_url = cfg.get("source", "")

    # أمر FFmpeg
    if is_slate:
        cmd = [
            "ffmpeg", "-stream_loop", "-1", "-re",
            "-i", SLATE_IMAGE_URL,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "libx264", "-preset", "superfast", "-tune", "stillimage",
            "-crf", "30",
            "-b:v", "500k", "-maxrate", "500k", "-bufsize", "1000k",
            "-c:a", "aac", "-b:a", "32k", "-ar", "44100", "-ac", "2",
            "-threads", "6",
            "-f", "flv", output_url
        ]
    else:
        cmd = [
            "ffmpeg",
            "-re",
            "-timeout", "5000000",
            "-i", input_url,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-flvflags", "no_duration_filesize",
            "-rtmp_live", "live",
            "-f", "flv", output_url
        ]

    # رسالة حالة
    msg = await context.bot.send_message(ADMIN_ID, f"⏳ جاري تشغيل {stream_id}...")
    msg_id = msg.message_id

    fail_count = 0
    max_fails = 10

    while fail_count < max_fails and not manual_stop_flags.get(stream_id):
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"❌ فشل تشغيل {stream_id}: {e}")
            return

        # انتظار قصير للتحقق من الفشل الفوري
        await asyncio.sleep(3)
        if process.returncode is not None:
            fail_count += 1
            delay = min(10 * fail_count, 60)
            await context.bot.edit_message_text(
                chat_id=ADMIN_ID, message_id=msg_id,
                text=f"❌ فشل سريع ({stream_id})\nالمحاولة {fail_count} من {max_fails} خلال {delay}s..."
            )
            await asyncio.sleep(delay)
            continue

        # الأزرار
        if is_slate:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 استئناف البث", callback_data=f"resume_{stream_id}"),
                 InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{stream_id}")]
            ])
            status_text = f"🟡 {stream_id} شاشة توقف"
        else:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("🟡 شاشة توقف", callback_data=f"slate_{stream_id}"),
                 InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{stream_id}"),
                 InlineKeyboardButton("🔄 تغيير المصدر", callback_data=f"change_{stream_id}")]
            ])
            status_text = f"✅ {stream_id} يعمل (نسخ مباشر)"

        try:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=status_text, reply_markup=buttons)
        except:
            pass

        active_streams[stream_id] = {
            "process": process,
            "frame_msg_id": msg_id,
            "manual_stop": False,
            "input_url": input_url
        }

        # مراقبة العملية
        try:
            while process.returncode is None and not manual_stop_flags.get(stream_id):
                await asyncio.sleep(15)
                try:
                    await context.bot.edit_message_text(
                        chat_id=ADMIN_ID, message_id=msg_id,
                        text=f"🟢 {stream_id} يعمل",
                        reply_markup=buttons
                    )
                except:
                    pass

            retcode = await process.wait()

            if manual_stop_flags.get(stream_id):
                await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"⏹ {stream_id} تم إيقاف البث يدوياً")
                break

            fail_count += 1
            delay = min(10 * fail_count, 60)
            await context.bot.edit_message_text(
                chat_id=ADMIN_ID, message_id=msg_id,
                text=f"⚠️ {stream_id} توقف (كود {retcode})\nإعادة المحاولة {fail_count} من {max_fails} خلال {delay} ثانية..."
            )
            await asyncio.sleep(delay)

        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"❌ {stream_id} خطأ: {e}")
            try: process.kill()
            except: pass
            break
        finally:
            if process.returncode is None:
                try: process.kill(); await process.wait()
                except: pass

    # تنظيف
    if stream_id in active_streams:
        del active_streams[stream_id]
    manual_stop_flags.pop(stream_id, None)

# ========== الأزرار التفاعلية ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_streams, manual_stop_flags
    query = update.callback_query
    await query.answer()
    data = query.data
    if not await check_admin(update): return

    # التعامل مع زر مراقبة السيرفر
    if data == "server_status":
        # إيقاف أي مراقبة سابقة
        if "status_task" in context.user_data:
            context.user_data["status_task"].cancel()
        task = asyncio.create_task(update_server_status(query, query.message.chat_id, query.message.message_id))
        context.user_data["status_task"] = task
        return

    if data == "main_menu":
        await query.edit_message_text("🖥️ **Rplay Server – 9 Streams**", reply_markup=main_menu_keyboard())
        return

    # تحليل الزر
    if "_" in data:
        parts = data.split("_", 1)
        action = parts[0]
        stream_id = parts[1] if len(parts) > 1 else ""

        if action == "menu" and stream_id:
            await query.edit_message_text(f"🎛️ التحكم في {stream_id}", reply_markup=stream_control_keyboard(stream_id))

        elif action == "start" and stream_id:
            cfg = streams_cfg.get(stream_id)
            if not cfg or not cfg.get("server") or not cfg.get("key"):
                await query.edit_message_text(f"❌ تحتاج إلى ضبط السيرفر والمفتاح لـ {stream_id} أولاً.")
                return
            if not cfg.get("source"):
                context.user_data["mode"] = f"source_{stream_id}"
                context.user_data["start_after_source"] = True
                await query.edit_message_text(f"📥 أرسل رابط المصدر لـ {stream_id}:")
            else:
                await query.edit_message_text(f"⏳ جاري تشغيل {stream_id}...")
                if stream_id in stream_tasks:
                    stream_tasks[stream_id].cancel()
                stream_tasks[stream_id] = asyncio.create_task(run_stream(stream_id, context))

        elif action == "stop" and stream_id:
            await stop_stream(stream_id, context.bot, manual=True)
            await query.edit_message_text(f"⏹ {stream_id} تم الإيقاف")

        elif action == "change" and stream_id:
            context.user_data["mode"] = f"source_{stream_id}"
            await query.edit_message_text(f"📥 أرسل رابط المصدر الجديد لـ {stream_id}:")

        elif action == "slate" and stream_id:
            if stream_id in active_streams:
                await stop_stream(stream_id, context.bot, manual=True)
                await query.edit_message_text(f"🟡 جاري تشغيل شاشة التوقف لـ {stream_id}...")
                stream_tasks[stream_id] = asyncio.create_task(run_stream(stream_id, context, is_slate=True))
            else:
                await query.edit_message_text(f"❌ {stream_id} لا يعمل حالياً")

        elif action == "resume" and stream_id:
            if stream_id in active_streams:
                await stop_stream(stream_id, context.bot, manual=True)
                await query.edit_message_text(f"🔙 جاري استئناف {stream_id}...")
                stream_tasks[stream_id] = asyncio.create_task(run_stream(stream_id, context))
            else:
                await query.edit_message_text(f"❌ لا يوجد مصدر محفوظ لـ {stream_id}")

        elif action == "settings" and stream_id:
            cfg = streams_cfg.get(stream_id, {})
            await query.edit_message_text(
                f"⚙️ إعدادات {stream_id}\n🔗 السيرفر: {cfg.get('server', 'غير محدد')}\n🔑 المفتاح: {cfg.get('key', 'غير محدد')}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("تعديل السيرفر", callback_data=f"setsrv_{stream_id}")],
                    [InlineKeyboardButton("تعديل المفتاح", callback_data=f"setkey_{stream_id}")],
                    [InlineKeyboardButton("🔙 رجوع", callback_data=f"menu_{stream_id}")],
                ])
            )

        elif action == "setsrv" and stream_id:
            context.user_data["mode"] = f"server_{stream_id}"
            await query.edit_message_text(f"🔗 أرسل رابط السيرفر الجديد لـ {stream_id}:")

        elif action == "setkey" and stream_id:
            context.user_data["mode"] = f"key_{stream_id}"
            await query.edit_message_text(f"🔑 أرسل المفتاح الجديد لـ {stream_id}:")

        elif action == "delete" and stream_id:
            streams_cfg[stream_id] = {"server": "", "key": "", "source": ""}
            save_streams_config(streams_cfg)
            await query.edit_message_text(f"🗑 تم حذف إعدادات {stream_id}")
    else:
        await query.answer("غير معروف")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Rplay Server – 9 Streams يعمل...")
    app.run_polling()