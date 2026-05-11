import asyncio, re, time, json, os, subprocess
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ========== الإعدادات ==========
with open("settings.json", "r") as f:
    settings = json.load(f)
    TOKEN = settings["TOKEN"]
    ADMIN_ID = settings["ADMIN_ID"]
    SLATE_IMAGE_URL = settings["SLATE_IMAGE_URL"]

CONFIG_FILE = "streams_config.json"
active_streams = {}
stream_tasks = {}
manual_stop_flags = {}
pending_source = {}

def load_streams_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except: pass
    default = {}
    for i in range(1, 10):
        default[f"stream_{i}"] = {"server": "", "key": ""}
    return default

def save_streams_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

streams_cfg = load_streams_config()

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
    except: return 0.0

def get_ram_usage():
    try:
        with open("/proc/meminfo", "r") as f:
            memtotal = 0; memavailable = 0
            for line in f:
                if line.startswith("MemTotal:"): memtotal = int(line.split()[1])
                elif line.startswith("MemAvailable:"): memavailable = int(line.split()[1])
            used = (memtotal - memavailable) // 1024
            total = memtotal // 1024
            return used, total
    except: return 0, 0

def get_disk_usage():
    try:
        result = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 4: return parts[2], parts[1]
    except: pass
    return "N/A", "N/A"

async def check_admin(update: Update) -> bool:
    if update.effective_user.id != ADMIN_ID:
        if update.message: await update.message.reply_text("🚫 هذا البوت مخصص للمالك فقط.")
        elif update.callback_query: await update.callback_query.answer("🚫 غير مصرح", show_alert=True)
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
    kb.append([InlineKeyboardButton("🖥️ مراقبة السيرفر", callback_data="server_status")])
    return InlineKeyboardMarkup(kb)

def control_menu(stream_id):
    running = stream_id in active_streams
    kb = []
    if not running:
        kb.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{stream_id}")])
    else:
        kb.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{stream_id}")])
    kb.append([InlineKeyboardButton("🔄 تغيير المصدر", callback_data=f"change_{stream_id}")])
    kb.append([InlineKeyboardButton("🟡 شاشة توقف", callback_data=f"slate_{stream_id}")])
    kb.append([InlineKeyboardButton("⚙️ إعدادات السيرفر/المفتاح", callback_data=f"settings_{stream_id}")])
    kb.append([InlineKeyboardButton("🗑 حذف الإعدادات", callback_data=f"delete_{stream_id}")])
    kb.append([InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

async def stop_stream(stream_id, bot, manual=False):
    global manual_stop_flags
    if stream_id in active_streams:
        if manual:
            manual_stop_flags[stream_id] = True  # تثبيت العلامة
        s = active_streams[stream_id]
        try: s["process"].kill()
        except: pass
        # لا نقوم بمسح العلامة هنا، بل ننتظر حتى تلتقطها run_stream
    # لا نلغي المهمة هنا، لأن المهمة نفسها هي التي ستلتقط العلامة

async def server_monitor(query, chat_id, msg_id):
    try:
        while True:
            cpu = get_cpu_usage()
            ram_u, ram_t = get_ram_usage()
            disk_u, disk_t = get_disk_usage()
            text = f"🖥️ **حالة السيرفر**\nCPU: {cpu:.1f}%\nRAM: {ram_u} MiB / {ram_t} MiB\nDisk: {disk_u} / {disk_t}"
            try: await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 تحديث الآن", callback_data="server_status"), InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")]]))
            except: pass
            await asyncio.sleep(5)
    except asyncio.CancelledError: pass

async def start(update, context):
    if not await check_admin(update): return
    await update.message.reply_text("🖥️ **Rplay Server – 9 Streams**", reply_markup=main_menu())

async def handle_message(update, context):
    if not await check_admin(update): return
    text = update.message.text.strip()
    mode = context.user_data.get("mode")
    if mode and "_" in mode:
        act, sid = mode.split("_", 1)
        if act == "source" and sid:
            context.user_data["mode"] = None
            pending_source[sid] = text
            await update.message.reply_text(f"⏳ جاري تشغيل Stream {sid.split('_')[1]}...")
            asyncio.create_task(run_stream(sid, context))
        elif act == "server" and sid:
            context.user_data["mode"] = None
            streams_cfg[sid]["server"] = text
            save_streams_config(streams_cfg)
            await update.message.reply_text("✅ تم حفظ السيرفر")
        elif act == "key" and sid:
            context.user_data["mode"] = None
            streams_cfg[sid]["key"] = text
            save_streams_config(streams_cfg)
            await update.message.reply_text("✅ تم حفظ المفتاح")

async def run_stream(stream_id, context, is_slate=False):
    global active_streams, manual_stop_flags
    cfg = streams_cfg.get(stream_id)
    if not cfg or not cfg.get("server") or not cfg.get("key"):
        await context.bot.send_message(ADMIN_ID, "❌ بيانات السيرفر/المفتاح غير موجودة")
        return
    input_url = pending_source.get(stream_id)
    if not is_slate and not input_url:
        await context.bot.send_message(ADMIN_ID, "❌ المصدر غير محدد")
        return
    if stream_id in active_streams:
        await context.bot.send_message(ADMIN_ID, "❌ البث يعمل بالفعل")
        return

    output_url = f"{cfg['server']}/{cfg['key']}"
    stream_num = stream_id.split("_")[1]
    name = f"Stream {stream_num}"

    if is_slate:
        cmd = ["ffmpeg", "-stream_loop", "-1", "-re", "-i", SLATE_IMAGE_URL,
               "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-c:v", "libx264", "-preset", "superfast", "-tune", "stillimage",
               "-crf", "30", "-b:v", "500k", "-maxrate", "500k", "-bufsize", "1000k",
               "-c:a", "aac", "-b:a", "32k", "-ar", "44100", "-ac", "2",
               "-threads", "6", "-f", "flv", output_url]
    else:
        cmd = ["ffmpeg", "-re", "-timeout", "5000000",
               "-i", input_url,
               "-c:v", "copy",
               "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
               "-flvflags", "no_duration_filesize", "-rtmp_live", "live",
               "-f", "flv", output_url]

    msg = await context.bot.send_message(ADMIN_ID, f"⏳ جاري تشغيل {name}...")
    mid = msg.message_id

    fail = 0
    while fail < 10 and not manual_stop_flags.get(stream_id):
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid, text=f"❌ فشل تشغيل {name}")
            break

        # فحص سريع للفشل
        err = ""
        try:
            while True:
                line = await asyncio.wait_for(proc.stderr.readline(), 5)
                if not line: break
                err = line.decode("utf-8", errors="ignore").strip()
        except asyncio.TimeoutError: pass
        await asyncio.sleep(1)

        if proc.returncode is not None:
            # إذا كان الإيقاف يدويًا أثناء الفحص، اخرج فورًا
            if manual_stop_flags.get(stream_id):
                break
            if "404" in err:
                txt = f"❌ المصدر غير موجود (404). تأكد من الرابط.\nالمصدر: {input_url}"
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 تغيير المصدر", callback_data=f"change_{stream_id}")],
                    [InlineKeyboardButton("🔙 القائمة", callback_data="main_menu")]
                ])
                await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid, text=txt, reply_markup=kb)
                break
            else:
                fail += 1
                delay = min(10 * fail, 60)
                await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid,
                    text=f"❌ فشل (كود {proc.returncode}). {err}\nالمحاولة {fail}/10 خلال {delay}s...")
                await asyncio.sleep(delay)
                continue

        # الأزرار
        if is_slate:
            btns = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 استئناف", callback_data=f"resume_{stream_id}"),
                                        InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{stream_id}")]])
            status = f"🟡 {name} شاشة توقف"
        else:
            btns = InlineKeyboardMarkup([[InlineKeyboardButton("🟡 شاشة توقف", callback_data=f"slate_{stream_id}"),
                                        InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{stream_id}"),
                                        InlineKeyboardButton("🔄 تغيير المصدر", callback_data=f"change_{stream_id}")]])
            status = f"🟢 {name} يعمل"

        try: await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid, text=status, reply_markup=btns)
        except: pass

        active_streams[stream_id] = {"process": proc, "frame_msg_id": mid, "manual_stop": False, "input_url": input_url}

        last_upd = time.time()
        try:
            while proc.returncode is None and not manual_stop_flags.get(stream_id):
                try:
                    line = await asyncio.wait_for(proc.stderr.readline(), 2)
                except asyncio.TimeoutError:
                    line = b""
                if line:
                    dec = line.decode("utf-8", errors="ignore").strip()
                    if "fps=" in dec:
                        now = time.time()
                        if now - last_upd >= 5:
                            last_upd = now
                            fps_m = re.search(r"fps=\s*([\d.]+)", dec)
                            time_m = re.search(r"time=(\d+:\d+:\d+\.\d+)", dec)
                            speed_m = re.search(r"speed=\s*([\d.]+)x", dec)
                            fps = fps_m.group(1) if fps_m else "0"
                            t = time_m.group(1) if time_m else "00:00:00"
                            sp = speed_m.group(1) if speed_m else "0"
                            txt = f"🟢 {name} يعمل\n📊 فريمات : {fps}\n⏰ الوقت : {t}\n🚀 سرعة الرفع : {sp}x"
                            try:
                                await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid, text=txt, reply_markup=btns)
                            except: pass
                await asyncio.sleep(0.1)

            # بعد الخروج من الحلقة الداخلية
            if manual_stop_flags.get(stream_id):
                await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid, text=f"⏹ {name} تم الإيقاف يدوياً")
                break  # يكسر الحلقة الخارجية مباشرة

            retcode = await proc.wait()
            fail += 1
            delay = min(10 * fail, 60)
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid,
                text=f"⚠️ {name} توقف (كود {retcode})\nإعادة {fail}/10 خلال {delay}s...")
            await asyncio.sleep(delay)

        except Exception as e:
            await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid, text=f"❌ {name} خطأ: {e}")
            try: proc.kill()
            except: pass
            break
        finally:
            if proc.returncode is None:
                try: proc.kill(); await proc.wait()
                except: pass

    # تنظيف نهائي
    if stream_id in active_streams: del active_streams[stream_id]
    manual_stop_flags.pop(stream_id, None)
    pending_source.pop(stream_id, None)

async def button_handler(update, context):
    global active_streams, manual_stop_flags
    q = update.callback_query
    await q.answer()
    d = q.data
    if not await check_admin(update): return

    if d == "server_status":
        if "status_task" in context.user_data: context.user_data["status_task"].cancel()
        context.user_data["status_task"] = asyncio.create_task(server_monitor(q, q.message.chat_id, q.message.message_id))
        return
    if d == "main_menu":
        await q.edit_message_text("🖥️ **Rplay Server – 9 Streams**", reply_markup=main_menu())
        return

    if "_" in d:
        act, sid = d.split("_", 1)
        num = sid.split("_")[1]
        if act == "menu":
            await q.edit_message_text(f"🎛️ Stream {num}", reply_markup=control_menu(sid))
        elif act == "start":
            cfg = streams_cfg.get(sid)
            if not cfg or not cfg.get("server") or not cfg.get("key"):
                await q.edit_message_text("❌ تحتاج لضبط السيرفر والمفتاح")
                return
            context.user_data["mode"] = f"source_{sid}"
            await q.edit_message_text(f"📥 أرسل رابط المصدر لـ Stream {num}:")
        elif act == "stop":
            # إيقاف يدوي
            if sid in active_streams:
                await stop_stream(sid, context.bot, manual=True)
                # سيتم عرض رسالة الإيقاف في run_stream نفسها
            else:
                await q.edit_message_text(f"❌ Stream {num} لا يعمل")
        elif act == "change":
            context.user_data["mode"] = f"source_{sid}"
            await q.edit_message_text(f"📥 أرسل رابط المصدر الجديد لـ Stream {num}:")
        elif act == "slate":
            if sid in active_streams:
                # لا نوقف البث يدوياً، سنوقف ثم نبدأ شاشة التوقف
                await stop_stream(sid, context.bot, manual=True)
                # انتظر قليلاً ثم ابدأ شاشة التوقف
                await asyncio.sleep(1)
                await q.edit_message_text(f"🟡 جاري شاشة التوقف لـ Stream {num}...")
                stream_tasks[sid] = asyncio.create_task(run_stream(sid, context, is_slate=True))
            else:
                await q.edit_message_text(f"❌ Stream {num} لا يعمل")
        elif act == "resume":
            if sid in active_streams:
                await stop_stream(sid, context.bot, manual=True)
                await asyncio.sleep(1)
                await q.edit_message_text(f"🔙 جاري استئناف Stream {num}...")
                context.user_data["mode"] = f"source_{sid}"
                await q.edit_message_text(f"📥 أرسل رابط المصدر لـ Stream {num}:")
            else:
                await q.edit_message_text("❌ لا يوجد مصدر محفوظ")
        elif act == "settings":
            cfg = streams_cfg.get(sid, {})
            await q.edit_message_text(
                f"⚙️ Stream {num}\n🔗 {cfg.get('server', 'غير محدد')}\n🔑 {cfg.get('key', 'غير محدد')}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("تعديل السيرفر", callback_data=f"setsrv_{sid}"),
                     InlineKeyboardButton("تعديل المفتاح", callback_data=f"setkey_{sid}")],
                    [InlineKeyboardButton("🔙 رجوع", callback_data=f"menu_{sid}")]]))
        elif act == "setsrv":
            context.user_data["mode"] = f"server_{sid}"
            await q.edit_message_text(f"🔗 أرسل رابط السيرفر لـ Stream {num}:")
        elif act == "setkey":
            context.user_data["mode"] = f"key_{sid}"
            await q.edit_message_text(f"🔑 أرسل المفتاح لـ Stream {num}:")
        elif act == "delete":
            streams_cfg[sid] = {"server": "", "key": ""}
            save_streams_config(streams_cfg)
            await q.edit_message_text(f"🗑 تم حذف إعدادات Stream {num}")
    else:
        await q.answer("غير معروف")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ 9 Streams Copy Ready")
    app.run_polling()