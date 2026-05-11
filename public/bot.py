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
    SLATE_IMAGE_URL = settings["SLATE_IMAGE_URL"]    # الصورة الثابتة الموثوقة

CONFIG_FILE = "stream_config.json"
active_stream = None
stream_lock = asyncio.Lock()
manual_stop_requested = False
current_logo = LOGO_URL_1

def load_stream_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except: pass
    return {}

def save_stream_config(server, key):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"server": server, "key": key}, f)

config = load_stream_config()

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
    if config.get("server") and config.get("key"):
        keyboard.append([InlineKeyboardButton("▶️ بدء البث (الشعار 1)", callback_data="start_logo1")])
        keyboard.append([InlineKeyboardButton("▶️ بدء البث (الشعار 2)", callback_data="start_logo2")])
    keyboard.append([InlineKeyboardButton("⚙️ إعدادات السيرفر والمفتاح", callback_data="settings")])
    return InlineKeyboardMarkup(keyboard)

async def stop_active_stream(bot, manual=False):
    global active_stream, manual_stop_requested
    async with stream_lock:
        if active_stream:
            if manual:
                manual_stop_requested = True
                active_stream["manual_stop"] = True
            try:
                active_stream["process"].kill()
            except: pass
            try:
                await active_stream["process"].wait()
            except: pass
            try:
                await bot.edit_message_text(
                    chat_id=ADMIN_ID,
                    message_id=active_stream["frame_msg_id"],
                    text="⏹ تم إيقاف البث"
                )
            except: pass
            active_stream = None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    await update.message.reply_text("🖥️ **Rplay Server**", reply_markup=main_menu_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    text = update.message.text.strip()

    if context.user_data.get("waiting_for_source"):
        context.user_data["waiting_for_source"] = False
        chosen_logo = context.user_data.get("selected_logo", LOGO_URL_1)
        context.user_data.pop("selected_logo", None)
        await stop_active_stream(context.bot, manual=True)
        await update.message.reply_text("⏳ جاري بدء البث بالمصدر الجديد...")
        asyncio.create_task(run_stream(context, text, logo_url=chosen_logo))
        return

    if context.user_data.get("waiting_for_server"):
        context.user_data["waiting_for_server"] = False
        config["server"] = text
        save_stream_config(config.get("server"), config.get("key"))
        await update.message.reply_text("✅ تم تحديث السيرفر")
        await start(update, context)
        return

    if context.user_data.get("waiting_for_key"):
        context.user_data["waiting_for_key"] = False
        config["key"] = text
        save_stream_config(config.get("server"), config.get("key"))
        await update.message.reply_text("✅ تم تحديث المفتاح")
        await start(update, context)
        return

async def run_stream(context: ContextTypes.DEFAULT_TYPE, input_url: str, logo_url: str = None, is_slate: bool = False):
    global active_stream, manual_stop_requested, current_logo
    manual_stop_requested = False

    if logo_url and not is_slate:
        current_logo = logo_url

    if not config.get("server") or not config.get("key"):
        await context.bot.send_message(ADMIN_ID, "❌ بيانات السيرفر/المفتاح غير موجودة")
        return

    output_url = f"{config['server']}/{config['key']}"

    # --- أمر FFmpeg مع خيارات إعادة الاتصال ومقاومة التقطع ---
    if is_slate:
        cmd = [
            "ffmpeg",
            "-stream_loop", "-1",
            "-re",
            "-i", SLATE_IMAGE_URL,
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
            "-reconnect", "1",                # إعادة الاتصال التلقائي
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-rw_timeout", "10000000",        # صبر 10 ثوانٍ على البيانات
            "-fflags", "+genpts+discardcorrupt",
            "-i", input_url,
            "-i", current_logo,
            "-filter_complex",
            "[1:v][0:v] scale2ref=iw:ih [logo][ref]; [ref][logo] overlay=0:0",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-b:v", "9000k",
            "-maxrate", "9000k",
            "-bufsize", "18000k",
            "-vsync", "cfr",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-f", "flv", output_url
        ]

    status_msg = await context.bot.send_message(ADMIN_ID, "⏳ جاري تشغيل البث...")
    msg_id = status_msg.message_id

    prev_usage = 0
    prev_time = 0

    while not manual_stop_requested:
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"❌ فشل تشغيل ffmpeg: {e}")
            return

        # الأزرار حسب الوضع
        if is_slate:
            buttons = [
                [InlineKeyboardButton("🔙 استئناف البث", callback_data="resume_stream"),
                 InlineKeyboardButton("⏹ إيقاف البث", callback_data="stop_stream")]
            ]
            status_text = "🟡 شاشة توقف"
        else:
            buttons = [
                [InlineKeyboardButton("🟡 شاشة توقف", callback_data="slate"),
                 InlineKeyboardButton("⏹ إيقاف البث", callback_data="stop_stream"),
                 InlineKeyboardButton("🔄 تغيير المصدر", callback_data="change_source")],
                [InlineKeyboardButton("🏷 الشعار 1", callback_data="logo_1"),
                 InlineKeyboardButton("🏷 الشعار 2", callback_data="logo_2")]
            ]
            status_text = "✅ تم بدء البث!"

        await context.bot.edit_message_text(
            chat_id=ADMIN_ID, message_id=msg_id, text=status_text,
            reply_markup=InlineKeyboardMarkup(buttons)
        )

        active_stream = {
            "process": process,
            "frame_msg_id": msg_id,
            "manual_stop": False,
            "input_url": input_url
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

                        cpu, mem_used, mem_total, new_usage, new_time = get_container_stats(prev_usage, prev_time)
                        prev_usage, prev_time = new_usage, new_time

                        if is_slate:
                            text = f"🟡 شاشة توقف\n📊 FPS: {fps}\n🖥 CPU: {cpu:.1f}% | RAM: {mem_used} MiB / {mem_total} MiB"
                        else:
                            time_match = re.search(r"time=(\d+:\d+:\d+\.\d+)", decoded)
                            speed_match = re.search(r"speed=\s*([\d.]+)x", decoded)
                            t = time_match.group(1) if time_match else "00:00:00"
                            sp = speed_match.group(1) if speed_match else "0"
                            text = (
                                f"🟢 Rplay Server يعمل\n"
                                f"📊 فريمات : {fps}\n"
                                f"⏰ الوقت : {t}\n"
                                f"🚀 سرعة الرفع : {sp}x\n"
                                f"🖥 CPU: {cpu:.1f}% | RAM: {mem_used} MiB / {mem_total} MiB"
                            )
                        try:
                            await context.bot.edit_message_text(
                                chat_id=ADMIN_ID, message_id=msg_id, text=text,
                                reply_markup=InlineKeyboardMarkup(buttons)
                            )
                        except: pass
                await asyncio.sleep(0.1)

            retcode = await process.wait()

            if manual_stop_requested:
                await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text="⏹ تم إيقاف البث يدوياً.")
                break

            await context.bot.edit_message_text(
                chat_id=ADMIN_ID, message_id=msg_id,
                text=f"⚠️ توقف البث (كود {retcode})، إعادة بعد 3 ثوانٍ..."
            )
            await asyncio.sleep(3)

        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"❌ خطأ: {e}")
            try: process.kill()
            except: pass
            break
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except: pass

    if active_stream:
        active_stream = None

# ========== الأزرار التفاعلية ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_stream, manual_stop_requested, current_logo
    query = update.callback_query
    await query.answer()
    data = query.data

    if not await check_admin(update): return

    if data == "start_logo1":
        if not config.get("server") or not config.get("key"):
            await query.edit_message_text("❌ تحتاج إلى ضبط السيرفر والمفتاح أولاً.")
            return
        context.user_data["selected_logo"] = LOGO_URL_1
        context.user_data["waiting_for_source"] = True
        await query.edit_message_text("📥 أرسل رابط المصدر لبدء البث (الشعار 1):")

    elif data == "start_logo2":
        if not config.get("server") or not config.get("key"):
            await query.edit_message_text("❌ تحتاج إلى ضبط السيرفر والمفتاح أولاً.")
            return
        context.user_data["selected_logo"] = LOGO_URL_2
        context.user_data["waiting_for_source"] = True
        await query.edit_message_text("📥 أرسل رابط المصدر لبدء البث (الشعار 2):")

    elif data == "logo_1":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            current_logo = LOGO_URL_1
            await query.edit_message_text("🔄 جاري التبديل إلى الشعار 1...")
            await asyncio.sleep(2)
            asyncio.create_task(run_stream(context, saved_input_url, LOGO_URL_1))
        else:
            await query.edit_message_text("❌ لا يوجد بث نشط لتغيير الشعار.")

    elif data == "logo_2":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            current_logo = LOGO_URL_2
            await query.edit_message_text("🔄 جاري التبديل إلى الشعار 2...")
            await asyncio.sleep(2)
            asyncio.create_task(run_stream(context, saved_input_url, LOGO_URL_2))
        else:
            await query.edit_message_text("❌ لا يوجد بث نشط لتغيير الشعار.")

    elif data == "settings":
        server = config.get("server", "غير محدد")
        key = config.get("key", "غير محدد")
        await query.edit_message_text(
            f"⚙️ **إعدادات Rplay Server**\n🔗 السيرفر: `{server}`\n🔑 المفتاح: `{key}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("تعديل السيرفر", callback_data="set_server")],
                [InlineKeyboardButton("تعديل المفتاح", callback_data="set_key")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")],
            ])
        )

    elif data == "set_server":
        context.user_data["waiting_for_server"] = True
        await query.edit_message_text("🔗 أرسل رابط السيرفر الجديد:")

    elif data == "set_key":
        context.user_data["waiting_for_key"] = True
        await query.edit_message_text("🔑 أرسل المفتاح الجديد:")

    elif data == "stop_stream":
        if active_stream:
            await stop_active_stream(context.bot, manual=True)
        else:
            await query.edit_message_text("❌ لا يوجد بث نشط.")

    elif data == "slate":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            await query.edit_message_text("🟡 جاري تشغيل شاشة التوقف...")
            asyncio.create_task(run_stream(context, saved_input_url, is_slate=True))
        else:
            await query.edit_message_text("❌ لا يوجد بث نشط أو المصدر غير معروف.")

    elif data == "resume_stream":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            await query.edit_message_text("🔙 جاري استئناف البث...")
            asyncio.create_task(run_stream(context, saved_input_url))
        else:
            await query.edit_message_text("❌ لا يوجد مصدر محفوظ للاستئناف.")

    elif data == "change_source":
        if active_stream:
            context.user_data["waiting_for_source"] = True
            await query.edit_message_text("📥 أرسل رابط المصدر الجديد:")
        else:
            await query.edit_message_text("❌ لا يوجد بث نشط.")

    elif data == "main_menu":
        await query.edit_message_text("🖥️ **Rplay Server**", reply_markup=main_menu_keyboard())

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Rplay Server يعمل...")
    app.run_polling()