# app.py (معدل)
# بوت تليجرام: إرسال ألبومات فيديو مؤقتة + تحقق اشتراك + إذاعة للمشرفين
import telebot
from telebot import types
import json
import time
import threading
import random
import string
import os
from telebot.apihelper import ApiTelegramException
import tempfile

# ---------------- CONFIG (محدث بناء على طلبك) ----------------
TOKEN = "8218231393:AAFpUEzTD2beyO1Ai91AyGHX8e5FsMlS7_"  # <-- توكن البوت الجديد
OWNER_ID = 8418469217  # ايدي الادمن/المالِك
BOT_USERNAME = "VTIGSBOT"  # اسم البوت بدون @
DATA_FILE = "deeiaIIta.json"

# زر "اضغط هنا" يقود لهذه القناة
SPECIFIC_CHANNEL_USERNAME = "vvhbkklbot"
SPECIFIC_CHANNEL_URL = f"https://t.me/{SPECIFIC_CHANNEL_USERNAME}"
SPECIFIC_CHANNEL_BUTTON_NAME = "قناة المقاطع"

# زر "قناتي" بعد عرض المقاطع
MAIN_CHANNEL_USERNAME = "femboy_IQ"
MAIN_CHANNEL_URL = f"https://t.me/{MAIN_CHANNEL_USERNAME}"
MAIN_CHANNEL_BUTTON_NAME = "قناتي"

# القنوات الإلزامية (تأكدت من عدم وجود id مكرر)
FORCED_CHANNELS_LIST = [
    {"id": -1003050689816, "username": "vvhbkklbot", "title": "القناة الاولى"},
    {"id": -1003086370700, "username": "FPILL1", "title": "القناة الثانية"},
    {"id": -1003675171238, "username": "ShanksIQ", "title": "القناة الثالثة"},
    {"id": -1002698918797, "username": "femboy_IQ", "title": "القناة الرابعة"},
    {"id": -1002342088361, "username": "pythonyemen1", "title": "القناة الخامسة"},    
    {"id": -1002798340303, "username": "LteiraQ", "title": "القناة السادسة"}
]

bot = telebot.TeleBot(TOKEN)

# مخزن مؤقت لطلب مشاركة الكود عندما يكون المستخدم غير مشترك بعد
PENDING_CODES = {}  # chat_id -> {"code": code, "requested_at": timestamp}

# حد أقصى للفيديوات لكل رابط (ألبوم واحد فقط)
MAX_VIDEOS_PER_GROUP = 10

# --- بنية البيانات الافتراضية ---
DEFAULT_DATA = {
    "admins": [OWNER_ID],
    "forced_channels": FORCED_CHANNELS_LIST,
    "video_groups": {},   # code -> [file_id, ...]
    "users": {},          # "user_id" -> {username, blocked, start_count, last_start_time, start_cooldown_until, last_link_time}
    "rose_messages": {},  # "chat_id" -> {"message_id": int, "likes": int, "dislikes": int}
    "temp_messages": {},  # "chat_id" -> [message_id, ...]  (رسائل بوت مؤقتة للحذف)
    "broadcast_ids": []   # قائمة أي ديهات محفوظة للإذاعات (لا تُحذف)
}

# استخدم RLock للسماح بالاستدعاءات المتداخلة أثناء الحفظ/القراءة
data_lock = threading.RLock()

# ---------------- storage utils ----------------
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # ensure keys exist
            for k, v in DEFAULT_DATA.items():
                if k not in data:
                    data[k] = v
            return data
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_DATA))

def save_data(data):
    # write atomically while تحت حماية القفل
    with data_lock:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="botdata_", suffix=".json", dir=".")
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            # استبدل الملف بشكل آمن
            os.replace(tmp_path, DATA_FILE)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

bot_data = load_data()

# --- ضمان تزامن forced channels من الكود إلى bot_data عند بدء التشغيل ---
with data_lock:
    try:
        existing = bot_data.get("forced_channels", []) or []
        merged = list(existing)

        def exists_in(ch, lst):
            for e in lst:
                try:
                    if e.get("id") == ch.get("id"):
                        return True
                    eu = (e.get("username") or "").lstrip('@').lower()
                    cu = (ch.get("username") or "").lstrip('@').lower()
                    if eu and cu and eu == cu:
                        return True
                except Exception:
                    continue
            return False

        for ch in FORCED_CHANNELS_LIST:
            if not exists_in(ch, merged):
                merged.append(ch)

        bot_data["forced_channels"] = merged
        save_data(bot_data)
    except Exception:
        pass

bot_data = load_data()  # reload to be safe

# --------- ضمان أن OWNER_ID موجود ضمن المشرفين وبصيغة صحيحة ----------
def normalize_admins_and_ensure_owner():
    with data_lock:
        admins = bot_data.get("admins", [])
        normalized = []
        for a in admins:
            try:
                normalized.append(int(a))
            except Exception:
                pass
        if int(OWNER_ID) not in normalized:
            normalized.append(int(OWNER_ID))
        bot_data["admins"] = list(dict.fromkeys(normalized))
        save_data(bot_data)

normalize_admins_and_ensure_owner()

# ---------------- cleanup scheduling ----------------
CLEANUP_TIMERS = {}  # chat_id -> threading.Timer
CLEANUP_DELAY = 10 * 60  # 10 دقائق

cleanup_timers_lock = threading.RLock()  # قفل منفصل لتنظيم مؤقتات التنظيف

def cancel_scheduled_cleanup(chat_id):
    with cleanup_timers_lock:
        t = CLEANUP_TIMERS.get(chat_id)
        if t:
            try:
                t.cancel()
            except Exception:
                pass
            try:
                del CLEANUP_TIMERS[chat_id]
            except Exception:
                pass

def schedule_user_cleanup(chat_id, delay=CLEANUP_DELAY):
    cancel_scheduled_cleanup(chat_id)
    t = threading.Timer(delay, cleanup_user, args=(chat_id,))
    t.daemon = True
    with cleanup_timers_lock:
        CLEANUP_TIMERS[chat_id] = t
    t.start()

def cleanup_user(chat_id):
    uid = str(chat_id)
    # لا تُحذف أي شيء من broadcast_ids
    try:
        with data_lock:
            temp_msgs = bot_data.get("temp_messages", {}).get(uid, [])
        for mid in list(temp_msgs):
            try:
                bot.delete_message(chat_id, mid)
            except Exception:
                pass
        with data_lock:
            if "temp_messages" in bot_data and uid in bot_data["temp_messages"]:
                del bot_data["temp_messages"][uid]

            if chat_id in PENDING_CODES:
                try:
                    del PENDING_CODES[chat_id]
                except Exception:
                    pass

            # حذف بيانات المستخدم من users و rose_messages لكن لا تمسح broadcast_ids
            if "users" in bot_data and uid in bot_data["users"]:
                del bot_data["users"][uid]
            if "rose_messages" in bot_data and uid in bot_data["rose_messages"]:
                del bot_data["rose_messages"][uid]

            save_data(bot_data)
    except Exception:
        pass

    try:
        with cleanup_timers_lock:
            if chat_id in CLEANUP_TIMERS:
                del CLEANUP_TIMERS[chat_id]
    except Exception:
        pass

# ---------------- safe send/edit helpers ----------------
MAX_MESSAGE_LENGTH = 4096

def _store_temp_message(chat_id, message_id):
    uid = str(chat_id)
    with data_lock:
        if "temp_messages" not in bot_data:
            bot_data["temp_messages"] = {}
        lst = bot_data["temp_messages"].get(uid, [])
        lst.append(message_id)
        bot_data["temp_messages"][uid] = lst
        save_data(bot_data)

def safe_send(chat_id, text, **kwargs):
    if text is None:
        return []
    sent_ids = []
    for i in range(0, len(text), MAX_MESSAGE_LENGTH):
        part = text[i:i + MAX_MESSAGE_LENGTH]
        try:
            msg = bot.send_message(chat_id, part, **kwargs)
            sent_ids.append(msg.message_id)
            try:
                _store_temp_message(chat_id, msg.message_id)
            except Exception:
                pass
        except ApiTelegramException as e:
            if "bot was blocked by the user" in str(e).lower() or "forbidden" in str(e).lower():
                try:
                    uid = str(chat_id)
                    with data_lock:
                        if uid in bot_data.get("users", {}):
                            bot_data[uid]["blocked"] = True
                            save_data(bot_data)
                except Exception:
                    pass
            continue
        except Exception:
            continue
    try:
        schedule_user_cleanup(chat_id)
    except Exception:
        pass
    return sent_ids

def safe_edit_message_text(chat_id, message_id, text, **kwargs):
    try:
        if text is None:
            return []
        if len(text) < MAX_MESSAGE_LENGTH:
            try:
                bot.edit_message_text(text, chat_id, message_id, **kwargs)
                _store_temp_message(chat_id, message_id)
                schedule_user_cleanup(chat_id)
                return [message_id]
            except ApiTelegramException as e:
                if "bot was blocked by the user" in str(e).lower() or "forbidden" in str(e).lower():
                    try:
                        uid = str(chat_id)
                        with data_lock:
                            if uid in bot_data.get("users", {}):
                                bot_data[uid]["blocked"] = True
                                save_data(bot_data)
                    except Exception:
                        pass
                pass
    except Exception:
        pass

    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass

    ids = safe_send(chat_id, text, **kwargs)
    return ids

# ---------------- helpers ----------------
def is_owner(user_id):
    try:
        return int(user_id) == int(OWNER_ID)
    except Exception:
        return False

def is_admin(user_id):
    try:
        uid = int(user_id)
    except Exception:
        return False
    with data_lock:
        admins = bot_data.get("admins", [])
        try:
            admin_ints = [int(x) for x in admins]
        except Exception:
            admin_ints = []
    return uid in admin_ints

def generate_unique_code(length=8):
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        with data_lock:
            if code not in bot_data.get("video_groups", {}):
                return code

def register_user(user):
    uid = str(user.id)
    with data_lock:
        if uid not in bot_data["users"]:
            bot_data["users"][uid] = {
                "username": user.username if user.username else "N/A",
                "blocked": False,
                "start_count": 0,
                "last_start_time": 0,
                "start_cooldown_until": 0,
                "last_link_time": 0
            }
        # أضف أي دي المستخدم إلى قائمة البث المحفوظة إن لم يكن موجوداً
        try:
            if "broadcast_ids" not in bot_data:
                bot_data["broadcast_ids"] = []
            if int(user.id) not in [int(x) for x in bot_data["broadcast_ids"]]:
                bot_data["broadcast_ids"].append(int(user.id))
        except Exception:
            pass
        save_data(bot_data)

def get_main_keyboard(user_id):
    markup = types.InlineKeyboardMarkup()
    if is_admin(user_id):
        markup.add(types.InlineKeyboardButton("➕ إضافة مقاطع", callback_data="admin_add_videos"))
        markup.add(types.InlineKeyboardButton("🗑️ حذف مقاطع", callback_data="admin_delete_videos"))
        markup.add(types.InlineKeyboardButton("📢 إرسال إذاعة", callback_data="admin_broadcast"))
        markup.add(types.InlineKeyboardButton("📊 إحصائيات البوت", callback_data="admin_stats"))
        # زر تصفير البوت للأدمن
        markup.add(types.InlineKeyboardButton("🔥 تصفير البوت بالكامل", callback_data="admin_wipe_bot"))
    return markup

def get_back_button(callback_data="admin_main_menu"):
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("🔙 رجوع", callback_data=callback_data))
    return mk

# ---------------- ROSE (وردة) helpers ----------------
def build_rose_markup_from_counts(likes, dislikes):
    mk = types.InlineKeyboardMarkup()
    like_btn = types.InlineKeyboardButton(f"{likes} 👍", callback_data="rose_like")
    dislike_btn = types.InlineKeyboardButton(f"{dislikes} 👎", callback_data="rose_dislike")
    mk.row(like_btn, dislike_btn)
    return mk

def send_fresh_rose(chat_id):
    uid = str(chat_id)
    with data_lock:
        data = bot_data.get("rose_messages", {}).get(uid, {"likes": 0, "dislikes": 0, "message_id": None})
    try:
        old_mid = data.get("message_id")
        if old_mid:
            try:
                bot.delete_message(chat_id, old_mid)
            except Exception:
                pass
            try:
                with data_lock:
                    if "temp_messages" in bot_data and uid in bot_data["temp_messages"]:
                        if old_mid in bot_data["temp_messages"][uid]:
                            bot_data["temp_messages"][uid].remove(old_mid)
            except Exception:
                pass
    except Exception:
        pass

    text = "🌺\nهل أعجبك المقطع؟"
    try:
        msg = bot.send_message(chat_id, text, reply_markup=build_rose_markup_from_counts(data.get("likes", 0), data.get("dislikes", 0)))
        with data_lock:
            if "rose_messages" not in bot_data:
                bot_data["rose_messages"] = {}
            bot_data["rose_messages"][uid] = {
                "message_id": msg.message_id,
                "likes": data.get("likes", 0),
                "dislikes": data.get("dislikes", 0)
            }
            try:
                _store_temp_message(chat_id, msg.message_id)
            except Exception:
                pass
            save_data(bot_data)
        try:
            schedule_user_cleanup(chat_id)
        except Exception:
            pass
        return msg.message_id
    except Exception:
        return None

def ensure_rose_exists(chat_id):
    uid = str(chat_id)
    with data_lock:
        if uid not in bot_data.get("rose_messages", {}) or not bot_data["rose_messages"][uid].get("message_id"):
            # نخرج من القفل ثم نرسل لأن send_fresh_rose سيقفل داخلياً
            pass
    send_fresh_rose(chat_id)

# ---------------- subscription check ----------------
def check_subscription(user_id):
    channels = bot_data.get("forced_channels", [])
    if not channels:
        return True, None

    unsubscribed = []
    seen_usernames = set()
    for ch in channels:
        uname = ch.get("username")
        title = ch.get("title", uname)
        if not uname:
            continue
        if uname in seen_usernames:
            continue
        seen_usernames.add(uname)
        try:
            member = bot.get_chat_member(f"@{uname}", user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                link = f"https://t.me/{uname}"
                unsubscribed.append({"title": title, "link": link})
        except Exception:
            link = f"https://t.me/{uname}"
            unsubscribed.append({"title": title, "link": link})
    if unsubscribed:
        return False, unsubscribed
    return True, None

def subscription_markup(unsubscribed_channels):
    mk = types.InlineKeyboardMarkup()
    for ch in unsubscribed_channels:
        mk.add(types.InlineKeyboardButton(f"اشترك في {ch['title']}", url=ch['link']))
    mk.add(types.InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_sub"))
    return mk

# ---------------- delete scheduler (existing) ----------------
def delete_messages_after_delay(chat_id, message_ids, delay=12):
    def task():
        time.sleep(delay)
        for mid in message_ids:
            try:
                bot.delete_message(chat_id, mid)
            except Exception:
                pass
    threading.Thread(target=task, daemon=True).start()

# ---------------- send media (core) ----------------
def send_media_for_code(chat_id, code, user_obj=None):
    with data_lock:
        groups = dict(bot_data.get("video_groups", {}))
    if code not in groups:
        safe_send(chat_id, "❌ عذراً، رابط المقاطع هذا غير صالح أو تم تعطيله.")
        return False

    video_file_ids = groups[code][:MAX_VIDEOS_PER_GROUP]
    sent_ids = []
    if video_file_ids:
        try:
            media = [types.InputMediaVideo(fid) for fid in video_file_ids]
            messages = bot.send_media_group(chat_id, media)
            for m in messages:
                sent_ids.append(m.message_id)
                try:
                    _store_temp_message(chat_id, m.message_id)
                except Exception:
                    pass
            time.sleep(0.2)
        except Exception:
            for fid in video_file_ids:
                try:
                    m = bot.send_video(chat_id, fid, caption="مقطع مؤقت")
                    sent_ids.append(m.message_id)
                    try:
                        _store_temp_message(chat_id, m.message_id)
                    except Exception:
                        pass
                    time.sleep(0.15)
                except Exception:
                    pass

    final_text = "شبيك صافن؟ حول مقاطع بسرعة قبل لا ينحذفن بعد 15 ثانية!!"
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton(MAIN_CHANNEL_BUTTON_NAME, url=MAIN_CHANNEL_URL))
    try:
        final_msg = bot.send_message(chat_id, final_text, reply_markup=mk)
        sent_ids.append(final_msg.message_id)
        try:
            _store_temp_message(chat_id, final_msg.message_id)
        except Exception:
            pass
    except Exception:
        pass

    send_fresh_rose(chat_id)
    delete_messages_after_delay(chat_id, sent_ids, delay=12)

    if user_obj:
        uid_str = str(user_obj.id)
        with data_lock:
            udata = bot_data.get("users", {}).get(uid_str, {})
            udata["last_link_time"] = time.time()
            bot_data["users"][uid_str] = udata
            save_data(bot_data)

    try:
        schedule_user_cleanup(chat_id)
    except Exception:
        pass

    return True

# ---------------- share link handler ----------------
def handle_share_link(message):
    chat_id = message.chat.id
    uid_str = str(chat_id)
    parts = message.text.split("_", 1)
    if len(parts) < 2:
        safe_send(chat_id, "❌ رابط غير صالح.")
        return
    code = parts[1].strip()

    register_user(message.from_user)
    with data_lock:
        user_data = dict(bot_data.get("users", {}).get(uid_str, {}))
    now = time.time()
    last_link = user_data.get("last_link_time", 0)
    if now - last_link < 60:
        safe_send(chat_id, "انتظر دقيقة وادخل لرابط المقاطع")
        return

    is_sub, unsub = check_subscription(message.from_user.id)
    if not is_sub:
        with data_lock:
            PENDING_CODES[chat_id] = {"code": code, "requested_at": now}
        try:
            bot.send_message(chat_id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub))
        except Exception:
            pass
        schedule_user_cleanup(chat_id)
        return

    success = send_media_for_code(chat_id, code, user_obj=message.from_user)
    if not success:
        safe_send(chat_id, "❌ حدث خطأ أثناء إرسال المقاطع. حاول لاحقاً")

    schedule_user_cleanup(chat_id)

# ---------------- /start handler ----------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.chat.type != "private":
        return

    register_user(message.from_user)
    uid_str = str(message.chat.id)
    with data_lock:
        user_data = dict(bot_data.get("users", {}).get(uid_str, {}))
    now = time.time()

    if message.text.startswith("/start _"):
        handle_share_link(message)
        return

    last_start = user_data.get("last_start_time", 0)
    start_count = user_data.get("start_count", 0)
    if now - last_start < 1:
        start_count += 1
    else:
        start_count = 1
    user_data["last_start_time"] = now
    user_data["start_count"] = start_count
    with data_lock:
        bot_data["users"][uid_str] = user_data
        save_data(bot_data)

    if start_count >= 3 and not user_data.get("blocked", False):
        user_data["start_cooldown_until"] = now + 3600
        with data_lock:
            bot_data["users"][uid_str] = user_data
            save_data(bot_data)

        freeze_text = '( لا ترسل /start سيتم تجاهلك ، اذا تحتاج مقاطع اذهب لقناة المقاطع واضغط على كلمة " اضغط هنا " 👇)'
        mk = types.InlineKeyboardMarkup()
        mk.add(types.InlineKeyboardButton(SPECIFIC_CHANNEL_BUTTON_NAME, url=SPECIFIC_CHANNEL_URL))
        safe_send(message.chat.id, "⚠️ تم تجميد حسابك لمدة ساعة بسبب إرسال /start بشكل متكرر وسريع.")
        safe_send(message.chat.id, freeze_text, reply_markup=mk)
        schedule_user_cleanup(message.chat.id)
        return

    if now < user_data.get("start_cooldown_until", 0):
        schedule_user_cleanup(message.chat.id)
        return

    is_sub, unsub_channels = check_subscription(message.chat.id)
    if not is_sub:
        try:
            bot.send_message(message.chat.id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub_channels))
        except Exception:
            pass
        schedule_user_cleanup(message.chat.id)
        return

    text = 'ادخل للقناة ودوس على " اضغط هنا " و ستظهر لك مقاطع 👇'
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton(SPECIFIC_CHANNEL_BUTTON_NAME, url=SPECIFIC_CHANNEL_URL))
    admin_kb = get_main_keyboard(message.chat.id)
    if getattr(admin_kb, "keyboard", None):
        for row in admin_kb.keyboard:
            try:
                mk.add(*row)
            except Exception:
                for btn in row:
                    mk.add(btn)
    try:
        bot.send_message(message.chat.id, text, reply_markup=mk)
    except Exception:
        pass

    schedule_user_cleanup(message.chat.id)

# ---------------- callback: تحقق الاشتراك ----------------
@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_sub_callback(call):
    register_user(call.from_user)
    is_sub, unsub = check_subscription(call.from_user.id)
    if is_sub:
        bot.answer_callback_query(call.id, "✅ تم التحقق، شكراً لاشتراكك!")
        pending = None
        with data_lock:
            pending = PENDING_CODES.get(call.message.chat.id)
        if pending:
            code = pending.get("code")
            try:
                with data_lock:
                    del PENDING_CODES[call.message.chat.id]
            except Exception:
                pass
            success = send_media_for_code(call.message.chat.id, code, user_obj=call.from_user)
            if not success:
                safe_send(call.message.chat.id, "❌ حدث خطأ أثناء إرسال المقاطع بعد التحقق. حاول لاحقاً.")
        else:
            send_welcome(call.message)
    else:
        bot.answer_callback_query(call.id, "❌ لم يتم الاشتراك بعد. يرجى الاشتراك أولاً.")
        try:
            safe_edit_message_text(call.message.chat.id, call.message.message_id,
                                   "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub))
        except Exception:
            try:
                bot.send_message(call.message.chat.id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub))
            except Exception:
                pass
    schedule_user_cleanup(call.message.chat.id)

# ---------------- admin: إحصائيات ----------------
@bot.callback_query_handler(func=lambda c: c.data == "admin_stats")
def admin_stats_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية الوصول.")
        return
    with data_lock:
        total = len(bot_data.get("users", {}))
        blocked = sum(1 for u in bot_data.get("users", {}).values() if u.get("blocked"))
        active = total - blocked
        video_groups_count = len(bot_data.get('video_groups', {}))
        broadcast_count = len(bot_data.get('broadcast_ids', []))
    text = (
        "📊 **إحصائيات البوت**\n"
        f"👥 إجمالي المستخدمين الذين دخلوا البوت: **{total}**\n"
        f"✅ المستخدمون النشطون: **{active}**\n"
        f"🚫 المستخدمون الذين حظروا البوت: **{blocked}**\n"
        f"🎬 عدد مجموعات المقاطع المخزنة: **{video_groups_count}**\n"
        f"📮 عدد عناوين البث المحفوظة: **{broadcast_count}**"
    )
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=get_back_button(), parse_mode="Markdown")
    schedule_user_cleanup(call.message.chat.id)

# ---------------- admin: broadcast (إذاعة) ----------------
bot.user_data = {}

@bot.callback_query_handler(func=lambda c: c.data == "admin_broadcast")
def admin_broadcast_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    try:
        msg = bot.edit_message_text("أرسل الآن الرسالة التي تريد إرسالها كإذاعة.\n\nيمكنك إرسال نص، صورة، فيديو، أو أي نوع من الرسائل.", call.message.chat.id, call.message.message_id, reply_markup=get_back_button())
    except Exception:
        try:
            msg = bot.send_message(call.message.chat.id, "أرسل الآن الرسالة التي تريد إرسالها كإذاعة.\n\nيمكنك إرسال نص، صورة، فيديو، أو أي نوع من الرسائل.", reply_markup=get_back_button())
        except Exception:
            return
    bot.register_next_step_handler(msg, process_broadcast_message)
    schedule_user_cleanup(call.message.chat.id)

def process_broadcast_message(message):
    if not is_admin(message.chat.id):
        return
    bot.user_data[message.chat.id] = {"broadcast_message": message}
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("✅ تأكيد الإرسال", callback_data="confirm_broadcast"))
    mk.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="admin_main_menu"))
    try:
        bot.send_message(message.chat.id, "هل أنت متأكد من إرسال هذه الرسالة كإذاعة لجميع العناوين المحفوظة؟", reply_markup=mk)
    except Exception:
        pass
    schedule_user_cleanup(message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "confirm_broadcast")
def confirm_broadcast_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    admin_id = call.from_user.id
    if admin_id not in bot.user_data or "broadcast_message" not in bot.user_data[admin_id]:
        bot.answer_callback_query(call.id, "❌ لم يتم العثور على رسالة الإذاعة. أعد المحاولة.")
        try:
            # محاولة العودة للقائمة الرئيسية (لو موجود)
            pass
        except Exception:
            pass
        return
    broadcast_message = bot.user_data[admin_id]["broadcast_message"]
    del bot.user_data[admin_id]

    # الآن نستخدم broadcast_ids (القائمة المحفوظة) لإرسال الإذاعة
    with data_lock:
        b_ids = list(bot_data.get("broadcast_ids", []))
    success = 0
    fail = 0
    for uid in list(b_ids):
        try:
            bot.forward_message(int(uid), broadcast_message.chat.id, broadcast_message.message_id)
            success += 1
            time.sleep(0.05)
        except Exception as e:
            try:
                err = str(e).lower()
                if "blocked" in err or "forbidden" in err or "deactivated" in err:
                    fail += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
    save_data(bot_data)
    try:
        bot.send_message(call.message.chat.id, f"✅ انتهت الإذاعة!\n\nتم الإرسال بنجاح إلى: {success} عنوان.\nفشل الإرسال إلى: {fail} عنوان.", reply_markup=get_back_button())
    except Exception:
        pass
    schedule_user_cleanup(call.message.chat.id)

# ---------------- admin: add/delete video groups ----------------
@bot.callback_query_handler(func=lambda c: c.data == "admin_add_videos")
def admin_add_videos_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    try:
        msg = bot.edit_message_text("أرسل الآن **عدد** الفيديوهات التي سترسلها (مثلاً: 5). الحد الأقصى لكل رابط هو 10.", call.message.chat.id, call.message.message_id, reply_markup=get_back_button())
    except Exception:
        try:
            msg = bot.send_message(call.message.chat.id, "أرسل الآن **عدد** الفيديوهات التي سترسلها (مثلاً: 5). الحد الأقصى لكل رابط هو 10.", reply_markup=get_back_button())
        except Exception:
            return
    bot.register_next_step_handler(msg, process_video_count)
    schedule_user_cleanup(call.message.chat.id)

def process_video_count(message):
    if not is_admin(message.chat.id):
        return
    try:
        count = int(message.text.strip())
        if count <= 0:
            raise ValueError
    except Exception:
        try:
            msg = bot.send_message(message.chat.id, "❌ العدد غير صحيح. أرسل عدد صحيح موجب (1-10).", reply_markup=get_back_button())
            bot.register_next_step_handler(msg, process_video_count)
        except Exception:
            pass
        schedule_user_cleanup(message.chat.id)
        return

    if count > MAX_VIDEOS_PER_GROUP:
        count = MAX_VIDEOS_PER_GROUP
        try:
            bot.send_message(message.chat.id, f"⚠️ تم ضبط العدد إلى الحد الأقصى ({MAX_VIDEOS_PER_GROUP}) لكل رابط.")
        except Exception:
            pass

    bot.user_data[message.chat.id] = {"state": "waiting_for_videos", "count": count, "received_videos": []}
    try:
        bot.send_message(message.chat.id, f"✅ الآن أرسل {count} فيديو (يفضل دفعة واحدة أو متتالية).", reply_markup=get_back_button())
    except Exception:
        pass
    schedule_user_cleanup(message.chat.id)

@bot.message_handler(content_types=['video'])
def process_videos_handler(message):
    if not is_admin(message.chat.id):
        return
    st = bot.user_data.get(message.chat.id)
    if not st or st.get("state") != "waiting_for_videos":
        return
    fid = message.video.file_id
    st["received_videos"].append(fid)
    remaining = st["count"] - len(st["received_videos"])
    try:
        if remaining > 0:
            bot.send_message(message.chat.id, f"✅ استلمت مقطع. المتبقي: {remaining}")
        else:
            code = generate_unique_code()
            with data_lock:
                bot_data["video_groups"][code] = st["received_videos"][:MAX_VIDEOS_PER_GROUP]
                save_data(bot_data)
            share_link = f"https://t.me/{BOT_USERNAME}?start=_{code}"
            bot.send_message(message.chat.id, f"🎉 تم استلام جميع المقاطع!\n\nرابط المشاركة:\n`{share_link}`", parse_mode="Markdown", reply_markup=get_main_keyboard(message.chat.id))
            del bot.user_data[message.chat.id]
    except Exception:
        pass
    schedule_user_cleanup(message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "admin_delete_videos")
def admin_delete_videos_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    with data_lock:
        groups = dict(bot_data.get("video_groups", {}))
    if not groups:
        bot.answer_callback_query(call.id, "لا توجد روابط مقاطع مخزنة.")
        try:
            # admin_main_menu_callback(call)  # إذا كانت هذه الدالة لديك نفذها
            pass
        except Exception:
            pass
        return
    mk = types.InlineKeyboardMarkup()
    for code, files in groups.items():
        mk.row(types.InlineKeyboardButton(f"عرض ({len(files)} مقطع)", callback_data=f"view_link_{code}"),
               types.InlineKeyboardButton("تعطيل", callback_data=f"disable_link_{code}"))
    mk.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="admin_main_menu"))
    try:
        bot.edit_message_text("اختر رابط لإدارته:", call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        try:
            bot.send_message(call.message.chat.id, "اختر رابط لإدارته:", reply_markup=mk)
        except Exception:
            pass
    schedule_user_cleanup(call.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("view_link_"))
def view_link_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    code = call.data.split("_", 2)[2]
    with data_lock:
        exists = code in bot_data.get("video_groups", {})
    if not exists:
        bot.answer_callback_query(call.id, "الرابط غير موجود.")
        return
    share_link = f"https://t.me/{BOT_USERNAME}?start=_{code}"
    safe_send(call.message.chat.id, f"رابط المقاطع:\n`{share_link}`", parse_mode="Markdown")
    bot.answer_callback_query(call.id, "تم إرسال الرابط.")
    schedule_user_cleanup(call.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("disable_link_"))
def disable_link_confirm(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    code = call.data.split("_", 2)[2]
    with data_lock:
        exists = code in bot_data.get("video_groups", {})
    if not exists:
        bot.answer_callback_query(call.id, "غير موجود.")
        return
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton(f"✅ تأكيد تعطيل {code}", callback_data=f"confirm_disable_link_{code}"))
    mk.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="admin_delete_videos"))
    try:
        bot.edit_message_text(f"هل أنت متأكد من تعطيل الرابط {code}؟", call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        try:
            bot.send_message(call.message.chat.id, f"هل أنت متأكد من تعطيل الرابط {code}؟", reply_markup=mk)
        except Exception:
            pass
    schedule_user_cleanup(call.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_disable_link_"))
def disable_link(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    code = call.data.split("_", 3)[3]
    with data_lock:
        if code in bot_data.get("video_groups", {}):
            del bot_data["video_groups"][code]
            save_data(bot_data)
            bot.answer_callback_query(call.id, f"✅ تم تعطيل {code}.")
        else:
            bot.answer_callback_query(call.id, "لم أجد الرابط.")
    try:
        admin_delete_videos_callback(call)
    except Exception:
        pass
    schedule_user_cleanup(call.message.chat.id)

# ---------------- ROSE callbacks (لايك / دسلايك) ----------------
@bot.callback_query_handler(func=lambda c: c.data in ["rose_like", "rose_dislike"])
def rose_callback(call):
    chat_id = call.message.chat.id
    uid = str(chat_id)
    with data_lock:
        if "rose_messages" not in bot_data:
            bot_data["rose_messages"] = {}
        data = bot_data["rose_messages"].get(uid, {"message_id": None, "likes": 0, "dislikes": 0})

        if call.data == "rose_like":
            data["likes"] = data.get("likes", 0) + 1
            bot.answer_callback_query(call.id, "👍 تم الإعجاب")
        else:
            data["dislikes"] = data.get("dislikes", 0) + 1
            bot.answer_callback_query(call.id, "👎 تم التصويت")

        bot_data["rose_messages"][uid] = data
        save_data(bot_data)

    try:
        old_mid = data.get("message_id")
        if old_mid:
            bot.delete_message(chat_id, old_mid)
            try:
                with data_lock:
                    if "temp_messages" in bot_data and uid in bot_data["temp_messages"] and old_mid in bot_data["temp_messages"][uid]:
                        bot_data["temp_messages"][uid].remove(old_mid)
            except Exception:
                pass
    except Exception:
        pass

    try:
        msg = bot.send_message(chat_id, "🌺\nهل أعجبك المقطع؟", reply_markup=build_rose_markup_from_counts(data.get("likes", 0), data.get("dislikes", 0)))
        with data_lock:
            bot_data["rose_messages"][uid]["message_id"] = msg.message_id
            try:
                _store_temp_message(chat_id, msg.message_id)
            except Exception:
                pass
            save_data(bot_data)
    except Exception:
        pass

    schedule_user_cleanup(chat_id)

# ---------------- admin: تصفير البوت (Wipe) ----------------
@bot.callback_query_handler(func=lambda c: c.data == "admin_wipe_bot")
def admin_wipe_bot_confirm(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية")
        return

    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("⚠️ نعم، احذف كل شيء", callback_data="confirm_wipe_bot"))
    mk.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="admin_main_menu"))

    try:
        bot.edit_message_text(
            "⚠️ تحذير خطير!\n\n"
            "سيتم:\n"
            "- حذف جميع بيانات المستخدمين\n"
            "- حذف جميع روابط المقاطع\n"
            "- حذف جميع الرسائل المؤقتة والوردات\n\n"
            "✅ سيتم الإبقاء فقط على ايديات الإذاعة\n\n"
            "هل أنت متأكد؟",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=mk
        )
    except Exception:
        try:
            bot.send_message(call.message.chat.id,
                "⚠️ تحذير خطير!\n\n"
                "سيتم:\n"
                "- حذف جميع بيانات المستخدمين\n"
                "- حذف جميع روابط المقاطع\n"
                "- حذف جميع الرسائل المؤقتة والوردات\n\n"
                "✅ سيتم الإبقاء فقط على ايديات الإذاعة\n\n"
                "هل أنت متأكد؟",
                reply_markup=mk
            )
        except Exception:
            pass
    schedule_user_cleanup(call.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "confirm_wipe_bot")
def admin_wipe_bot_execute(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية")
        return

    # اجمع الايديات المحفوظة والرسائل المؤقتة قبل المسح
    with data_lock:
        preserved_broadcast_ids = list(bot_data.get("broadcast_ids", []))
        temp_messages_copy = dict(bot_data.get("temp_messages", {}))

    # حاول حذف كل الرسائل المؤقتة المسجلة
    try:
        for uid_str, mids in temp_messages_copy.items():
            try:
                chat_id = int(uid_str)
            except Exception:
                # قد تكون المفاتيح ليست أرقام - تجاهل
                continue
            for mid in list(mids):
                try:
                    bot.delete_message(chat_id, mid)
                except Exception:
                    pass
    except Exception:
        pass

    # أوقف كل مؤقتات التنظيف
    try:
        with cleanup_timers_lock:
            for k, t in list(CLEANUP_TIMERS.items()):
                try:
                    t.cancel()
                except Exception:
                    pass
            CLEANUP_TIMERS.clear()
    except Exception:
        pass

    # الآن نعيد بناء bot_data ونحفظ
    with data_lock:
        bot_data.clear()
        bot_data.update({
            "admins": [OWNER_ID],
            "forced_channels": FORCED_CHANNELS_LIST,
            "video_groups": {},
            "users": {},
            "rose_messages": {},
            "temp_messages": {},
            "broadcast_ids": preserved_broadcast_ids
        })
        save_data(bot_data)

    try:
        bot.answer_callback_query(call.id, "✅ تم تصفير البوت بنجاح")
    except Exception:
        pass

    try:
        bot.send_message(
            call.message.chat.id,
            "✅ **تم حذف جميع بيانات البوت بنجاح**\n\n"
            "📌 ما بقي محفوظ:\n"
            "- ايديات الإذاعة فقط\n\n"
            "🔄 البوت الآن نظيف وجاهز للعمل من جديد",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(call.from_user.id)
        )
    except Exception:
        pass

    schedule_user_cleanup(call.message.chat.id)

# ---------------- catch-all handler (خاص فقط) ----------------
@bot.message_handler(func=lambda m: True, content_types=['text','photo','video','document','sticker','audio','voice'])
def handle_all_messages(message):
    if message.chat.type != "private":
        return

    register_user(message.from_user)
    uid_str = str(message.chat.id)
    with data_lock:
        user_data = dict(bot_data.get("users", {}).get(uid_str, {}))
    now = time.time()
    last_link = user_data.get("last_link_time", 0)
    if now - last_link < 60:
        schedule_user_cleanup(message.chat.id)
        return

    try:
        bot.send_message(message.chat.id, "الرجاء استخدام الأزرار أدناه:", reply_markup=get_main_keyboard(message.chat.id))
    except Exception:
        pass

    schedule_user_cleanup(message.chat.id)

# ---------------- run ----------------
if __name__ == "__main__":
    print("Bot is starting...")
    try:
        bot.polling(none_stop=True)
    except Exception as e:
        print("Polling error:", e)
        time.sleep(5)
        bot.polling(none_stop=True)
