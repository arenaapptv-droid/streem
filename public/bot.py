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

CONFIG_FILE = "stream_config.json"
active_stream = None
stream_lock = asyncio.Lock()
manual_stop_requested = False
current_logo = LOGO_URL_1
stream_task = None  # المهمة الحالية

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
    global active_stream, manual_stop_requested, stream_task
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
        # إلغاء المهمة القديمة بشكل آمن
        if stream_task:
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
            stream_task = None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    await update.message.reply_text("🖥️ **Rplay Server**", reply_markup=main_menu_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global stream_task
    if not await check_admin(update): return
    text = update.message.text.strip()

    if context.user_data.get("waiting_for_source"):
        context.user_data["waiting_for_source"] = False
        chosen_logo = context.user_data.get("selected_logo", LOGO_URL_1)
        context.user_data.pop("selected_logo", None)

        # منع تشغيل بث جديد إذا كان هناك بث نشط
        if stream_task and not stream_task.done():
            await update.message.reply_text("❌ بث آخر قيد التشغيل حالياً. أوقفه أولاً.")
            return

        await stop_active_stream(context.bot, manual=True)
        await update.message.reply_text("⏳ جاري بدء البث بالمصدر الجديد...")
        stream_task = asyncio.create_task(run_stream(context, text, logo_url=chosen_logo))
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

    # --- أمر FFmpeg المحسّن (مع 6 خيوط) ---
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
            "-rw_timeout", "10000000",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-i", input_url,
            "-i", current_logo,
            "-filter_complex",
            "[1:v][0:v] scale2ref=iw:ih [logo][ref]; [ref][logo] overlay=0:0",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-g", "50",
            "-r", "30",
            "-crf", "24",
            "-b:v", "3500k",
            "-maxrate", "3500k",
            "-bufsize", "7000k",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-flvflags", "no_duration_filesize",
            "-rtmp_live", "live",
            "-threads", "6",
            "-f", "flv", output_url
        ]

    status_msg = await context.bot.send_message(ADMIN_ID, "⏳ جاري تشغيل البث...")
    msg_id = status_msg.message_id

    fail_count = 0
    max_fails = 10

    while not manual_stop_requested and fail_count < max_fails:
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL  # لا نقرأ stderr لتجنب التعليق
            )
        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"❌ فشل تشغيل ffmpeg: {e}")
            return

        # التحقق من الفشل السريع
        await asyncio.sleep(3)
        if process.returncode is not None:
            fail_count += 1
            delay = min(10 * fail_count, 60)
            await context.bot.edit_message_text(
                chat_id=ADMIN_ID, message_id=msg_id,
                text=f"❌ فشل سريع (كود {process.returncode}). المحاولة {fail_count} من {max_fails} خلال {delay}s..."
            )
            await asyncio.sleep(delay)
            continue

        # نجح: إعداد الأزرار
        if is_slate:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 استئناف البث", callback_data="resume_stream"),
                 InlineKeyboardButton("⏹ إيقاف البث", callback_data="stop_stream")]
            ])
            status_text = "🟡 شاشة توقف"
        else:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("🟡 شاشة توقف", callback_data="slate"),
                 InlineKeyboardButton("⏹ إيقاف البث", callback_data="stop_stream"),
                 InlineKeyboardButton("🔄 تغيير المصدر", callback_data="change_source")],
                [InlineKeyboardButton("🏷 الشعار 1", callback_data="logo_1"),
                 InlineKeyboardButton("🏷 الشعار 2", callback_data="logo_2")]
            ])
            status_text = "✅ تم بدء البث!"

        await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=status_text, reply_markup=buttons)

        active_stream = {"process": process, "frame_msg_id": msg_id, "manual_stop": False, "input_url": input_url}

        # مراقبة العملية بدون stderr
        try:
            while process.returncode is None and not manual_stop_requested:
                await asyncio.sleep(15)  # تحديث كل 15 ثانية
                try:
                    await context.bot.edit_message_text(
                        chat_id=ADMIN_ID,
                        message_id=msg_id,
                        text="🟢 البث يعمل",
                        reply_markup=buttons
                    )
                except:
                    pass

            retcode = await process.wait()

            if manual_stop_requested:
                await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text="⏹ تم إيقاف البث يدوياً.")
                break

            fail_count += 1
            delay = min(10 * fail_count, 60)
            await context.bot.edit_message_text(
                chat_id=ADMIN_ID, message_id=msg_id,
                text=f"⚠️ توقف البث (كود {retcode})\nإعادة المحاولة {fail_count} من {max_fails} خلال {delay} ثانية..."
            )
            await asyncio.sleep(delay)

        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg_id, text=f"❌ خطأ: {e}")
            try: process.kill()
            except: pass
            break
        finally:
            if process.returncode is None:
                try: process.kill(); await process.wait()
                except: pass

    if active_stream:
        active_stream = None

# ========== الأزرار التفاعلية ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_stream, manual_stop_requested, current_logo, stream_task
    query = update.callback_query
    await query.answer()
    data = query.data
    if not await check_admin(update): return

    # منع تشغيل بث جديد إذا كان هناك بث نشط
    if data.startswith("start_") or data.startswith("logo") or data.startswith("resume") or data == "slate":
        if stream_task and not stream_task.done():
            await query.edit_message_text("❌ بث آخر قيد التشغيل. أوقفه أولاً.")
            return

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
            stream_task = asyncio.create_task(run_stream(context, saved_input_url, LOGO_URL_1))
        else: await query.edit_message_text("❌ لا يوجد بث نشط لتغيير الشعار.")
    elif data == "logo_2":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            current_logo = LOGO_URL_2
            await query.edit_message_text("🔄 جاري التبديل إلى الشعار 2...")
            await asyncio.sleep(2)
            stream_task = asyncio.create_task(run_stream(context, saved_input_url, LOGO_URL_2))
        else: await query.edit_message_text("❌ لا يوجد بث نشط لتغيير الشعار.")
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
        if active_stream: await stop_active_stream(context.bot, manual=True)
        else: await query.edit_message_text("❌ لا يوجد بث نشط.")
    elif data == "slate":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            await query.edit_message_text("🟡 جاري تشغيل شاشة التوقف...")
            stream_task = asyncio.create_task(run_stream(context, saved_input_url, is_slate=True))
        else: await query.edit_message_text("❌ لا يوجد بث نشط.")
    elif data == "resume_stream":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            await query.edit_message_text("🔙 جاري استئناف البث...")
            stream_task = asyncio.create_task(run_stream(context, saved_input_url))
        else: await query.edit_message_text("❌ لا يوجد مصدر محفوظ.")
    elif data == "change_source":
        if active_stream:
            context.user_data["waiting_for_source"] = True
            await query.edit_message_text("📥 أرسل رابط المصدر الجديد:")
        else: await query.edit_message_text("❌ لا يوجد بث نشط.")
    elif data == "main_menu":
        await query.edit_message_text("🖥️ **Rplay Server**", reply_markup=main_menu_keyboard())

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Rplay Server Ultra Stable...")
    app.run_polling()