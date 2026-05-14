"""
بوت تلجرام لإدارة البث المباشر (HLS / RTMP)
المتطلبات: pip install python-telegram-bot aiohttp psutil
"""

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from collections import defaultdict

from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ═══════════════════════════════════════════
#  إعدادات
# ═══════════════════════════════════════════
with open("settings.json") as f:
    cfg = json.load(f)

TOKEN      = cfg["TOKEN"]
ADMIN_IDS  = cfg.get("ADMIN_IDS", [cfg.get("ADMIN_ID")])   # قائمة أدمن
BASE_URL   = cfg.get("BASE_URL", "http://164.68.102.28")
HTTP_PORT  = cfg.get("PORT", 8080)
HLS_DIR    = cfg.get("HLS_DIR", "/tmp/hls")
DB_FILE    = "streams.json"

os.makedirs(HLS_DIR, exist_ok=True)

# ═══════════════════════════════════════════
#  قاعدة البيانات (JSON بسيط)
# ═══════════════════════════════════════════
streams: dict = {}      # sid -> stream dict
processes: dict = {}    # sid -> asyncio.subprocess
viewers: dict   = defaultdict(set)   # sid -> {ip}
viewer_ts: dict = defaultdict(dict)  # sid -> {ip: timestamp}

STREAM_DEFAULTS = {
    "name": "",
    "source": "",
    "type": "hls",          # hls | rtmp
    "mode": "copy",         # copy | encode
    "active": False,
    "fps": "—",
    "ua": "ExoPlayerLib/2.18.5",
    "logo": "",
    "rtmp_server": "",
    "rtmp_key": "",
    "chat_id": None,
    "message_id": None,
    "start_time": 0,
}

def _load_db():
    global streams
    if not os.path.exists(DB_FILE):
        return
    with open(DB_FILE) as f:
        raw = json.load(f)
    for sid, s in raw.items():
        streams[sid] = {**STREAM_DEFAULTS, **s, "active": False}

def _save_db():
    data = {}
    for sid, s in streams.items():
        tmp = {k: v for k, v in s.items()}
        tmp.pop("active", None)
        data[sid] = tmp
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

_load_db()

# ═══════════════════════════════════════════
#  صلاحيات
# ═══════════════════════════════════════════
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ═══════════════════════════════════════════
#  خادم HLS الداخلي
# ═══════════════════════════════════════════
async def _hls_handler(request: web.Request):
    sid      = request.match_info["sid"]
    filename = request.match_info.get("file", "index.m3u8")
    path     = os.path.join(HLS_DIR, sid, filename)
    if not os.path.exists(path):
        return web.Response(status=404)
    ip = request.remote or "unknown"
    viewers[sid].add(ip)
    viewer_ts[sid][ip] = time.time()
    return web.FileResponse(path)

async def start_http_server():
    app = web.Application()
    app.router.add_get("/live/{sid}/{file:.*}", _hls_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    print(f"[HTTP] يعمل على المنفذ {HTTP_PORT}")

# ═══════════════════════════════════════════
#  معلومات النظام
# ═══════════════════════════════════════════
def system_info() -> str:
    lines = [f"📡 البثوث النشطة: {sum(1 for s in streams.values() if s.get('active'))} / {len(streams)}"]
    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=0.1)
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        lines += [
            f"🖥️  CPU : {cpu:.1f}%",
            f"🧠 RAM : {mem.percent:.1f}%  ({mem.used//1024**2} MB / {mem.total//1024**2} MB)",
            f"💾 Disk: {disk.percent:.1f}%  ({disk.used//1024**3} GB / {disk.total//1024**3} GB)",
        ]
    except ImportError:
        lines.append("⚠️ psutil غير مثبت (pip install psutil)")
    return "\n".join(lines)

# ═══════════════════════════════════════════
#  مراقبة السيرفر (live update)
# ═══════════════════════════════════════════
_monitor: dict = {}   # chat_id -> {"msg_id": int, "task": Task}

async def _monitor_loop(bot, chat_id: int, msg_id: int):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف المراقبة", callback_data="mon:stop")]])
    while True:
        try:
            await bot.edit_message_text(system_info(), chat_id=chat_id, message_id=msg_id, reply_markup=kb)
        except Exception:
            pass
        await asyncio.sleep(3)

def _stop_monitor(chat_id: int):
    if chat_id in _monitor:
        _monitor[chat_id]["task"].cancel()
        del _monitor[chat_id]

# ═══════════════════════════════════════════
#  لوحة تحكم البث
# ═══════════════════════════════════════════
def _stream_text(sid: str) -> str:
    s = streams[sid]
    active   = s.get("active", False)
    uptime   = "—"
    if active and s.get("start_time"):
        uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - s["start_time"]))
    vc = len(viewers.get(sid, set()))

    status_icon = "🟢 يعمل" if active else "🔴 متوقف"
    mode_label  = "نسخ مباشر" if s["mode"] == "copy" else "ترميز"

    lines = [
        f"🎛 *{s['name']}*",
        f"",
        f"📶 الحالة : {status_icon}",
        f"⚙️  الوضع  : {mode_label}",
        f"🎬 FPS    : {s.get('fps', '—')}",
        f"👥 مشاهدون: {vc}",
        f"⏱ مدة التشغيل: {uptime}",
        f"",
        f"📥 المصدر : `{s['source'] or 'غير محدد'}`",
        f"🕵️ UA     : `{s.get('ua')}`",
        f"🖼 شعار   : {'✅' if s.get('logo') else '❌'}",
    ]
    if s["type"] == "hls":
        lines.append(f"\n🔗 `{BASE_URL}:{HTTP_PORT}/live/{sid}/index.m3u8`")
    else:
        rtmp = f"{s.get('rtmp_server','')}/{s.get('rtmp_key','')}"
        lines.append(f"\n📡 `{rtmp}`")
    return "\n".join(lines)

def _stream_kb(sid: str) -> InlineKeyboardMarkup:
    s      = streams[sid]
    active = s.get("active", False)
    mode   = s.get("mode", "copy")
    typ    = s.get("type", "hls")

    toggle_btn = (
        InlineKeyboardButton("⏹ إيقاف",  callback_data=f"s:stop:{sid}")
        if active else
        InlineKeyboardButton("▶️ تشغيل", callback_data=f"s:start:{sid}")
    )
    mode_btn = InlineKeyboardButton(
        "⚙️ تبديل → ترميز"    if mode == "copy" else "⚙️ تبديل → نسخ",
        callback_data=f"s:mode:{sid}"
    )
    rows = [
        [toggle_btn],
        [
            InlineKeyboardButton("📥 مصدر",        callback_data=f"e:source:{sid}"),
            InlineKeyboardButton("🖼 شعار",         callback_data=f"e:logo:{sid}"),
        ],
        [
            InlineKeyboardButton("🕵️ UA",           callback_data=f"e:ua:{sid}"),
            InlineKeyboardButton("✏️ تسمية",        callback_data=f"e:name:{sid}"),
        ],
        [mode_btn],
    ]
    if typ == "rtmp":
        rows.append([
            InlineKeyboardButton("📡 خادم RTMP",   callback_data=f"e:rtmp_server:{sid}"),
            InlineKeyboardButton("🔑 مفتاح RTMP",  callback_data=f"e:rtmp_key:{sid}"),
        ])
    rows.append([InlineKeyboardButton("🗑 حذف البث", callback_data=f"s:del:{sid}")])
    rows.append([InlineKeyboardButton("🔙 رجوع",     callback_data="nav:back")])
    return InlineKeyboardMarkup(rows)

async def _refresh_panel(bot, sid: str):
    s = streams.get(sid)
    if not s or not s.get("chat_id") or not s.get("message_id"):
        return
    try:
        await bot.edit_message_text(
            _stream_text(sid),
            chat_id=s["chat_id"],
            message_id=s["message_id"],
            reply_markup=_stream_kb(sid),
            parse_mode="Markdown",
        )
    except Exception as e:
        if "not modified" not in str(e).lower():
            print(f"[panel] {e}")

# ═══════════════════════════════════════════
#  بناء أوامر ffmpeg
# ═══════════════════════════════════════════
_BASE_FFMPEG = [
    "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
    "-re",
    "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
    "-timeout", "10000000", "-rw_timeout", "10000000",
    "-fflags", "+genpts+discardcorrupt",
    "-analyzeduration", "5000000", "-probesize", "50000000",
    "-stream_loop", "-1",
]

def _build_cmd(sid: str) -> list:
    s    = streams[sid]
    src  = s["source"]
    ua   = s.get("ua", "ExoPlayerLib/2.18.5")
    logo = s.get("logo", "")
    mode = s["mode"]
    typ  = s["type"]

    base = _BASE_FFMPEG + ["-user_agent", ua]

    encode_video = [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-b:v", "6000k", "-maxrate", "6000k", "-bufsize", "12000k",
        "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
        "-vsync", "cfr", "-r", "30", "-g", "60",
    ]
    encode_audio = ["-c:a", "aac", "-b:a", "128k"]
    copy_video   = ["-c:v", "copy"]
    copy_audio   = ["-c:a", "aac", "-b:a", "128k"]

    if typ == "hls":
        out_dir  = os.path.join(HLS_DIR, sid)
        os.makedirs(out_dir, exist_ok=True)
        out_file = os.path.join(out_dir, "index.m3u8")
        hls_opts = [
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments+append_list",
            "-y", out_file,
        ]
        if mode == "copy":
            return base + ["-i", src] + copy_video + copy_audio + hls_opts
        # encode مع شعار
        if logo:
            return (base + ["-i", src, "-i", logo,
                    "-filter_complex", "[1:v]scale=120:-1[lg];[0:v][lg]overlay=10:10"]
                    + encode_video + encode_audio + hls_opts)
        return base + ["-i", src] + encode_video + encode_audio + hls_opts

    else:  # RTMP
        rtmp_url = f"{s['rtmp_server']}/{s['rtmp_key']}"
        rtmp_opts = ["-f", "flv", "-y", rtmp_url]
        if mode == "copy":
            return base + ["-i", src] + copy_video + copy_audio + rtmp_opts
        if logo:
            return (base + ["-i", src, "-i", logo,
                    "-filter_complex", "[1:v]scale=120:-1[lg];[0:v][lg]overlay=10:10"]
                    + encode_video + encode_audio + rtmp_opts)
        return base + ["-i", src] + encode_video + encode_audio + rtmp_opts

# ═══════════════════════════════════════════
#  تشغيل / إيقاف البث
# ═══════════════════════════════════════════
async def stream_start(sid: str, bot):
    s   = streams[sid]
    cmd = _build_cmd(sid)

    # إيقاف أي عملية قديمة
    await _kill(sid)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    processes[sid] = proc
    s["active"]     = True
    s["start_time"] = time.time()
    s["fps"]        = "—"
    _save_db()
    await _refresh_panel(bot, sid)

    # قراءة مخرجات ffmpeg لاستخراج FPS
    async def _read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            txt = line.decode(errors="ignore")
            m = re.search(r"fps=\s*([\d.]+)", txt)
            if m:
                s["fps"] = m.group(1)

    asyncio.create_task(_read_stderr())

    # انتظر العملية وعند انتهائها أعد تشغيلها (watchdog)
    await proc.wait()
    if s.get("active"):           # إذا لم يوقفه المستخدم → أعد التشغيل
        print(f"[watchdog] إعادة تشغيل {sid}")
        await asyncio.sleep(3)
        asyncio.create_task(stream_start(sid, bot))
    else:
        s["active"] = False
        processes.pop(sid, None)
        _save_db()
        await _refresh_panel(bot, sid)

async def _kill(sid: str):
    proc = processes.pop(sid, None)
    if proc:
        try:
            proc.terminate()
            await asyncio.sleep(0.5)
            proc.kill()
        except Exception:
            pass

async def stream_stop(sid: str, bot):
    streams[sid]["active"] = False
    await _kill(sid)
    shutil.rmtree(os.path.join(HLS_DIR, sid), ignore_errors=True)
    _save_db()
    await _refresh_panel(bot, sid)

# ═══════════════════════════════════════════
#  لوحات التنقل
# ═══════════════════════════════════════════
MAIN_KB = ReplyKeyboardMarkup(
    [["📺 HLS", "📡 RTMP"], ["➕ إضافة بث", "🖥 مراقبة"], ["🧹 تنظيف"]],
    resize_keyboard=True,
)

def _list_kb(typ: str) -> InlineKeyboardMarkup:
    rows = []
    for sid, s in streams.items():
        if s["type"] != typ:
            continue
        icon = "🟢" if s.get("active") else "🔴"
        rows.append([InlineKeyboardButton(f"{icon}  {s['name']}", callback_data=f"nav:open:{sid}")])
    if not rows:
        rows.append([InlineKeyboardButton("— لا توجد بثوث —", callback_data="noop")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="nav:back")])
    return InlineKeyboardMarkup(rows)

def _main_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 قائمة HLS",  callback_data="nav:hls"),
         InlineKeyboardButton("📡 قائمة RTMP", callback_data="nav:rtmp")],
        [InlineKeyboardButton("➕ إضافة بث",   callback_data="nav:add")],
        [InlineKeyboardButton("🖥 مراقبة",      callback_data="mon:start")],
        [InlineKeyboardButton("🧹 تنظيف",       callback_data="nav:clean")],
    ])

# ═══════════════════════════════════════════
#  أمر /start
# ═══════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 غير مصرح لك")
    await update.message.reply_text(
        "🎬 *بوت البث المباشر*\n\n"
        "📺 HLS  — بث لأجهزة التلفزيون والمتصفحات\n"
        "📡 RTMP — بث ليوتيوب / فيسبوك / تويتش\n"
        "➕ إضافة بث جديد من خلال الأزرار\n"
        "🖥 مراقبة موارد السيرفر في الوقت الفعلي",
        reply_markup=MAIN_KB,
        parse_mode="Markdown",
    )

# ═══════════════════════════════════════════
#  معالج الرسائل النصية
# ═══════════════════════════════════════════
async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = update.message.text.strip()
    ud   = context.user_data

    # ── أزرار الرد الرئيسية ──────────────────
    if text == "📺 HLS":
        return await update.message.reply_text("📺 قائمة HLS:", reply_markup=_list_kb("hls"))
    if text == "📡 RTMP":
        return await update.message.reply_text("📡 قائمة RTMP:", reply_markup=_list_kb("rtmp"))
    if text == "➕ إضافة بث":
        ud["step"] = "add:name"
        return await update.message.reply_text("📝 أرسل اسم البث الجديد:")
    if text == "🧹 تنظيف":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        return await update.message.reply_text("✅ تم تنظيف ملفات HLS")
    if text == "🖥 مراقبة":
        cid = update.message.chat_id
        _stop_monitor(cid)
        kb  = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف", callback_data="mon:stop")]])
        msg = await update.message.reply_text(system_info(), reply_markup=kb)
        task = asyncio.create_task(_monitor_loop(context.bot, cid, msg.message_id))
        _monitor[cid] = {"msg_id": msg.message_id, "task": task}
        return

    # ── مراحل إضافة بث ──────────────────────
    step = ud.get("step", "")

    if step == "add:name":
        name = text
        sid  = re.sub(r"\W+", "_", name).lower() or str(uuid.uuid4())[:8]
        # تفادي التكرار
        base, c = sid, 1
        while sid in streams:
            sid = f"{base}_{c}"; c += 1
        streams[sid] = {**STREAM_DEFAULTS, "name": name}
        ud["step"] = "add:source"
        ud["sid"]  = sid
        return await update.message.reply_text("📥 أرسل رابط المصدر:")

    if step == "add:source":
        sid = ud.get("sid")
        if sid and sid in streams:
            streams[sid]["source"] = text
            _save_db()
            ud.pop("step"); ud.pop("sid")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📺 HLS",  callback_data=f"settype:{sid}:hls"),
                InlineKeyboardButton("📡 RTMP", callback_data=f"settype:{sid}:rtmp"),
            ]])
            return await update.message.reply_text("اختر نوع البث:", reply_markup=kb)

    # ── تعديل حقل بث ────────────────────────
    if "edit" in ud:
        field, sid = ud.pop("edit")
        s = streams.get(sid)
        if s:
            if text != "/skip":
                s[field] = text
            _save_db()
            # أعد تشغيل البث إذا كان يعمل ومصدره تغيّر
            if s.get("active") and field in ("source", "logo", "ua", "mode"):
                asyncio.create_task(stream_stop(sid, context.bot))
                await asyncio.sleep(1)
                asyncio.create_task(stream_start(sid, context.bot))
            else:
                await _refresh_panel(context.bot, sid)
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    await update.message.reply_text("استخدم الأزرار أو اختر من القائمة.")

# ═══════════════════════════════════════════
#  معالج Callback (الأزرار المضمنة)
# ═══════════════════════════════════════════
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    cid  = q.message.chat_id
    mid  = q.message.message_id
    uid  = q.from_user.id

    try:
        await q.answer()
    except Exception:
        pass

    if not is_admin(uid):
        return

    # ── تحديد نوع البث عند الإضافة ──────────
    if data.startswith("settype:"):
        _, sid, typ = data.split(":")
        if sid in streams:
            streams[sid]["type"] = typ
            _save_db()
            if typ == "rtmp":
                context.user_data["edit"] = ("rtmp_server", sid)
                return await q.edit_message_text("📡 أرسل خادم RTMP (مثال: rtmp://a.rtmp.youtube.com/live2):")
            # HLS → افتح اللوحة مباشرة
            streams[sid]["chat_id"]    = cid
            streams[sid]["message_id"] = mid
            _save_db()
            return await q.edit_message_text(
                _stream_text(sid),
                reply_markup=_stream_kb(sid),
                parse_mode="Markdown",
            )
        return

    # ── التنقل ──────────────────────────────
    if data == "nav:back" or data == "noop":
        await q.edit_message_text("🎬 القائمة الرئيسية", reply_markup=_main_inline_kb())
        return
    if data == "nav:hls":
        return await q.edit_message_text("📺 قائمة HLS:", reply_markup=_list_kb("hls"))
    if data == "nav:rtmp":
        return await q.edit_message_text("📡 قائمة RTMP:", reply_markup=_list_kb("rtmp"))
    if data == "nav:add":
        context.user_data["step"] = "add:name"
        return await q.edit_message_text("📝 أرسل اسم البث الجديد:")
    if data == "nav:clean":
        shutil.rmtree(HLS_DIR, ignore_errors=True)
        os.makedirs(HLS_DIR, exist_ok=True)
        return await q.edit_message_text("✅ تم تنظيف ملفات HLS", reply_markup=_main_inline_kb())

    if data.startswith("nav:open:"):
        sid = data[9:]
        if sid in streams:
            streams[sid]["chat_id"]    = cid
            streams[sid]["message_id"] = mid
            _save_db()
            return await q.edit_message_text(
                _stream_text(sid),
                reply_markup=_stream_kb(sid),
                parse_mode="Markdown",
            )

    # ── مراقبة ──────────────────────────────
    if data == "mon:start":
        _stop_monitor(cid)
        kb   = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف", callback_data="mon:stop")]])
        await q.edit_message_text(system_info(), reply_markup=kb)
        task = asyncio.create_task(_monitor_loop(context.bot, cid, mid))
        _monitor[cid] = {"msg_id": mid, "task": task}
        return
    if data == "mon:stop":
        _stop_monitor(cid)
        return await q.edit_message_text(system_info(), reply_markup=_main_inline_kb())

    # ── تحكم البث ───────────────────────────
    if data.startswith("s:"):
        _, action, sid = data.split(":", 2)

        if action == "start":
            s = streams.get(sid)
            if not s:
                return await q.answer("❌ بث غير موجود", show_alert=True)
            if not s.get("source"):
                return await q.answer("❌ حدد مصدر البث أولاً", show_alert=True)
            if s["type"] == "rtmp" and not (s.get("rtmp_server") and s.get("rtmp_key")):
                return await q.answer("❌ أكمل إعدادات RTMP", show_alert=True)
            if s.get("active"):
                return await q.answer("⚠️ البث يعمل بالفعل", show_alert=True)
            await q.answer("⏳ جاري التشغيل...")
            asyncio.create_task(stream_start(sid, context.bot))
            return

        if action == "stop":
            await q.answer("⏹ جاري الإيقاف...")
            asyncio.create_task(stream_stop(sid, context.bot))
            return

        if action == "mode":
            s = streams.get(sid)
            if s:
                s["mode"] = "encode" if s["mode"] == "copy" else "copy"
                _save_db()
                await q.answer(f"✅ الوضع: {'ترميز' if s['mode']=='encode' else 'نسخ مباشر'}")
                if s.get("active"):
                    asyncio.create_task(stream_stop(sid, context.bot))
                    await asyncio.sleep(1)
                    asyncio.create_task(stream_start(sid, context.bot))
                else:
                    await _refresh_panel(context.bot, sid)
            return

        if action == "del":
            if sid in streams:
                await stream_stop(sid, context.bot)
                del streams[sid]
                _save_db()
            return await q.edit_message_text("🗑 تم حذف البث", reply_markup=_main_inline_kb())

    # ── تعديل حقول البث ─────────────────────
    if data.startswith("e:"):
        _, field, sid = data.split(":", 2)
        PROMPTS = {
            "source":      "📥 أرسل رابط المصدر الجديد:",
            "logo":        "🖼 أرسل رابط الشعار (أو /skip للحذف):",
            "ua":          "🕵️ أرسل User-Agent (أو /skip للافتراضي):",
            "name":        "✏️ أرسل الاسم الجديد:",
            "rtmp_server": "📡 أرسل خادم RTMP:",
            "rtmp_key":    "🔑 أرسل مفتاح البث:",
        }
        if field in PROMPTS:
            context.user_data["edit"] = (field, sid)
            streams[sid]["chat_id"]    = cid
            streams[sid]["message_id"] = mid
            _save_db()
            return await q.edit_message_text(PROMPTS[field])

# ═══════════════════════════════════════════
#  نقطة الدخول
# ═══════════════════════════════════════════
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_http_server())

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    app.add_handler(CallbackQueryHandler(cb_handler))

    print("🚀 البوت يعمل...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()