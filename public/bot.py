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

# ========== الإعدادات الأساسية ==========
with open("settings.json") as f:
    cfg = json.load(f)
TOKEN = cfg["TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]
BASE_URL = cfg.get("BASE_URL", "http://164.68.102.28")
PORT = cfg.get("PORT", 8080)
LOGO_URL_1 = cfg.get("LOGO_URL_1", "")
LOGO_URL_2 = cfg.get("LOGO_URL_2", "")
SLATE_VIDEO_URL = cfg.get("SLATE_VIDEO_URL", "https://files.catbox.moe/4liy8i.mp4")

HLS_DIR = "/tmp/hls"
STREAMS_FILE = "streams.json"
CONFIG_FILE = "stream_config.json"

os.makedirs(HLS_DIR, exist_ok=True)

# ========== بيانات البثوث ==========
streams = {}
processes = {}
viewers = defaultdict(set)
viewer_last = defaultdict(dict)
active_stream = None          # للبث الرئيسي (Rplay Server)
stream_lock = asyncio.Lock()
manual_stop_requested = False
current_logo = LOGO_URL_1

# ========== تحميل الإعدادات ==========
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

def save_streams():
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
        status = system_status()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف المراقبة", callback_data="stop_monitor")]])
        try:
            await bot.edit_message_text(status, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
        except:
            pass
        await asyncio.sleep(1)

# ========== واجهة المستخدم ==========
reply_kb = ReplyKeyboardMarkup([
    ["📺 قائمة HLS", "📡 قائمة RTMP"],
    ["➕ إضافة بث", "🖥 مراقبة السيرفر"],
    ["🧹 تنظيف الملفات"]
], resize_keyboard=True)

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

# ========== تشغيل البث (أوامر Rplay Server الأصلية) ==========
async def run_stream(context: ContextTypes.DEFAULT_TYPE, input_url: str, logo_url: str = None, is_slate: bool = False):
    global active_stream, manual_stop_requested, current_logo
    manual_stop_requested = False

    if logo_url and not is_slate:
        current_logo = logo_url

    if not config.get("server") or not config.get("key"):
        await context.bot.send_message(ADMIN_ID, "❌ بيانات السيرفر/المفتاح غير موجودة")
        return

    output_url = f"{config['server']}/{config['key']}"

    if is_slate:
        cmd = [
            "ffmpeg",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-stream_loop", "-1",
            "-re",
            "-i", SLATE_VIDEO_URL,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-b:v", "5000k",
            "-maxrate", "5000k",
            "-bufsize", "10000k",
            "-c:a", "aac",
            "-b:a", "128k",
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

                        try:
                            with open("/proc/loadavg") as f:
                                load = f.read().split()[0]
                        except:
                            load = "0"

                        if is_slate:
                            text = f"🟡 شاشة توقف\n📊 FPS: {fps}\n🖥 الحمل: {load}"
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
                                f"🖥 الحمل: {load}"
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

# ========== تشغيل بث متعدد (HLS/RTMP) ==========
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

    if typ == "hls":
        if mode == "copy":
            cmd = [
                "ffmpeg", "-re",
                "-user_agent", ua,
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "10000000",
                "-fflags", "+genpts+discardcorrupt",
                "-i", src,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", "-y", out_file
            ]
        else:
            if logo and len(logo) > 5:
                cmd = [
                    "ffmpeg", "-re",
                    "-user_agent", ua,
                    "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                    "-rw_timeout", "10000000",
                    "-fflags", "+genpts+discardcorrupt",
                    "-i", src,
                    "-i", logo,
                    "-filter_complex",
                    "[1:v][0:v] scale2ref=iw:ih [logo][ref]; [ref][logo] overlay=0:0",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
                    "-vsync", "cfr", "-r", "30",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                    "-hls_flags", "delete_segments", "-y", out_file
                ]
            else:
                cmd = [
                    "ffmpeg", "-re",
                    "-user_agent", ua,
                    "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                    "-rw_timeout", "10000000",
                    "-fflags", "+genpts+discardcorrupt",
                    "-i", src,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
                    "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
                    "-vsync", "cfr", "-r", "30",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                    "-hls_flags", "delete_segments", "-y", out_file
                ]
    else:  # RTMP
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        if mode == "copy":
            cmd = [
                "ffmpeg", "-re",
                "-user_agent", ua,
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "10000000",
                "-fflags", "+genpts+discardcorrupt",
                "-i", src,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-f", "flv", "-y", rtmp_url
            ]
        else:
            if logo and len(logo) > 5:
                cmd = [
                    "ffmpeg", "-re",
                    "-user_agent", ua,
                    "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                    "-rw_timeout", "10000000",
                    "-fflags", "+genpts+discardcorrupt",
                    "-i", src,
                    "-i", logo,
                    "-filter_complex",
                    "[1:v][0:v] scale2ref=iw:ih [logo][ref]; [ref][logo] overlay=0:0",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
                    "-vsync", "cfr", "-r", "30",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    "-f", "flv", "-y", rtmp_url
                ]
            else:
                cmd = [
                    "ffmpeg", "-re",
                    "-user_agent", ua,
                    "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                    "-rw_timeout", "10000000",
                    "-fflags", "+genpts+discardcorrupt",
                    "-i", src,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-b:v", "9000k", "-maxrate", "9000k", "-bufsize", "18000k",
                    "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
                    "-vsync", "cfr", "-r", "30",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
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
    save_streams()
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
    asyncio.create_task(read_stderr())
    await proc.wait()

    s["active"] = False
    processes.pop(sid, None)
    save_streams()
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
        save_streams()
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

    # أزرار القائمة الرئيسية
    if data == "main_menu":
        if monitor_active:
            monitor_active = False
            if monitor_task: monitor_task.cancel()
        await q.edit_message_text("🎬 القائمة الرئيسية", reply_markup=reply_kb)
        return

    if data == "stop_monitor":
        if monitor_active:
            monitor_active = False
            if monitor_task: monitor_task.cancel()
        await q.edit_message_text(system_status(), reply_markup=reply_kb)
        return

    if data == "list_hls":
        await q.edit_message_text("📺 قائمة HLS:", reply_markup=streams_list("hls"))
        return
    if data == "list_rtmp":
        await q.edit_message_text("📡 قائمة RTMP:", reply_markup=streams_list("rtmp"))
        return

    # فتح لوحة بث
    if data.startswith("open_"):
        sid = data[5:]
        if sid in streams:
            streams[sid]["chat_id"] = chat_id
            streams[sid]["message_id"] = msg_id
            save_streams()
            await update_panel(sid, context.bot)
        else:
            await q.edit_message_text("❌ البث غير موجود")
        return

    # تشغيل بث متعدد
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

    # إيقاف بث متعدد
    if data.startswith("stop_"):
        sid = data[5:]
        await q.answer("⏹ تم الإيقاف")
        asyncio.create_task(stop_stream(sid, context.bot))
        return

    # تعديلات البث (مصدر، شعار، UA، إلخ)
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

    # تبديل وضع التشغيل (نسخ/ترميز)
    if data.startswith("mode_"):
        sid = data[5:]
        if sid in streams:
            old = streams[sid]["mode"]
            new = "encode" if old == "copy" else "copy"
            streams[sid]["mode"] = new
            save_streams()
            await q.answer(f"✅ تم التبديل إلى {'ترميز' if new=='encode' else 'نسخ'}")
            if streams[sid].get("active"):
                await stop_stream(sid, context.bot)
                asyncio.create_task(start_stream(sid, context.bot))
            else:
                await update_panel(sid, context.bot)
        else:
            await q.answer("❌ خطأ", show_alert=True)
        return

    # حذف بث
    if data.startswith("del_"):
        sid = data[4:]
        await stop_stream(sid, context.bot)
        if sid in streams:
            del streams[sid]
            save_streams()
        await q.edit_message_text("🗑 تم حذف البث", reply_markup=reply_kb)
        return

    # ===== أزرار Rplay Server الرئيسية =====
    if data == "start_logo1":
        if not config.get("server") or not config.get("key"):
            await q.edit_message_text("❌ تحتاج إلى ضبط السيرفر والمفتاح أولاً.")
            return
        context.user_data["selected_logo"] = LOGO_URL_1
        context.user_data["waiting_for_source"] = True
        await q.edit_message_text("📥 أرسل رابط المصدر لبدء البث (الشعار 1):")
        return

    if data == "start_logo2":
        if not config.get("server") or not config.get("key"):
            await q.edit_message_text("❌ تحتاج إلى ضبط السيرفر والمفتاح أولاً.")
            return
        context.user_data["selected_logo"] = LOGO_URL_2
        context.user_data["waiting_for_source"] = True
        await q.edit_message_text("📥 أرسل رابط المصدر لبدء البث (الشعار 2):")
        return

    if data == "settings":
        server = config.get("server", "غير محدد")
        key = config.get("key", "غير محدد")
        await q.edit_message_text(
            f"⚙️ **إعدادات Rplay Server**\n🔗 السيرفر: `{server}`\n🔑 المفتاح: `{key}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("تعديل السيرفر", callback_data="set_server")],
                [InlineKeyboardButton("تعديل المفتاح", callback_data="set_key")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")],
            ])
        )
        return

    if data == "set_server":
        context.user_data["waiting_for_server"] = True
        await q.edit_message_text("🔗 أرسل رابط السيرفر الجديد:")
        return

    if data == "set_key":
        context.user_data["waiting_for_key"] = True
        await q.edit_message_text("🔑 أرسل المفتاح الجديد:")
        return

    if data == "stop_stream":
        if active_stream:
            await stop_active_stream(context.bot, manual=True)
        else:
            await q.edit_message_text("❌ لا يوجد بث نشط.")
        return

    if data == "slate":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            await q.edit_message_text("🟡 جاري تشغيل شاشة التوقف...")
            asyncio.create_task(run_stream(context, saved_input_url, is_slate=True))
        else:
            await q.edit_message_text("❌ لا يوجد بث نشط أو المصدر غير معروف.")
        return

    if data == "resume_stream":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            await q.edit_message_text("🔙 جاري استئناف البث...")
            asyncio.create_task(run_stream(context, saved_input_url))
        else:
            await q.edit_message_text("❌ لا يوجد مصدر محفوظ للاستئناف.")
        return

    if data == "change_source":
        if active_stream:
            context.user_data["waiting_for_source"] = True
            await q.edit_message_text("📥 أرسل رابط المصدر الجديد:")
        else:
            await q.edit_message_text("❌ لا يوجد بث نشط.")
        return

    if data == "logo_1":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            current_logo = LOGO_URL_1
            await q.edit_message_text("🔄 جاري التبديل إلى الشعار 1...")
            await asyncio.sleep(2)
            asyncio.create_task(run_stream(context, saved_input_url, LOGO_URL_1))
        else:
            await q.edit_message_text("❌ لا يوجد بث نشط لتغيير الشعار.")
        return

    if data == "logo_2":
        if active_stream and active_stream.get("input_url"):
            saved_input_url = active_stream["input_url"]
            await stop_active_stream(context.bot, manual=True)
            current_logo = LOGO_URL_2
            await q.edit_message_text("🔄 جاري التبديل إلى الشعار 2...")
            await asyncio.sleep(2)
            asyncio.create_task(run_stream(context, saved_input_url, LOGO_URL_2))
        else:
            await q.edit_message_text("❌ لا يوجد بث نشط لتغيير الشعار.")
        return

# ========== معالجة الرسائل النصية ==========
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return
    text = update.message.text
    chat_id = update.message.chat_id

    # أزرار الرد الرئيسية
    if text == "📺 قائمة HLS":
        await update.message.reply_text("📺 قائمة HLS:", reply_markup=streams_list("hls"))
        return
    if text == "📡 قائمة RTMP":
        await update.message.reply_text("📡 قائمة RTMP:", reply_markup=streams_list("rtmp"))
        return
    if text == "➕ إضافة بث":
        context.user_data["step"] = "add_name"
        await update.message.reply_text("📝 أرسل اسم البث الجديد:")
        return
    if text == "🖥 مراقبة السيرفر":
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
    if text == "🧹 تنظيف الملفات":
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
        save_streams()
        context.user_data["step"] = "add_source"
        context.user_data["sid"] = sid
        await update.message.reply_text("📥 أرسل رابط المصدر:")
        return

    # إضافة بث جديد: المصدر
    if context.user_data.get("step") == "add_source":
        sid = context.user_data.get("sid")
        if sid and sid in streams:
            streams[sid]["source"] = text
            save_streams()
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
            save_streams()
            context.user_data.pop("edit")
            await update_panel(sid, context.bot)
            await update.message.delete()
        return

    # ===== معالجة طلبات Rplay Server =====
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
        await start_command(update, context)
        return

    if context.user_data.get("waiting_for_key"):
        context.user_data["waiting_for_key"] = False
        config["key"] = text
        save_stream_config(config.get("server"), config.get("key"))
        await update.message.reply_text("✅ تم تحديث المفتاح")
        await start_command(update, context)
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
            save_streams()
            await update_panel(sid, context.bot)
    else:
        await q.edit_message_text("❌ خطأ")

# ========== أمر البدء ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 غير مصرح")
        return
    await update.message.reply_text("🖥️ **Rplay Server**", reply_markup=main_menu_keyboard())

def main_menu_keyboard():
    keyboard = []
    if config.get("server") and config.get("key"):
        keyboard.append([InlineKeyboardButton("▶️ بدء البث (الشعار 1)", callback_data="start_logo1")])
        keyboard.append([InlineKeyboardButton("▶️ بدء البث (الشعار 2)", callback_data="start_logo2")])
    keyboard.append([InlineKeyboardButton("⚙️ إعدادات السيرفر والمفتاح", callback_data="settings")])
    keyboard.append([InlineKeyboardButton("🎛️ إدارة البثوث", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

# ========== التشغيل ==========
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_http())

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(callback_handler, pattern="^(?!settype_).*"))
    app.add_handler(CallbackQueryHandler(set_type_callback, pattern="^settype_"))

    print("🚀 البوت يعمل...")
    app.run_polling()

if __name__ == "__main__":
    main()