# -*- coding: utf-8 -*-
"""
بوت تليجرام لإرسال ألبومات فيديو مؤقتة (حتى 10 فيديو) ثم حذفها بعد 15 ثانية.
ضع التوكن الجديد في TOKEN قبل التشغيل.
"""

import telebot
from telebot import types
import json
import time
import threading
import random
import string
import os
import tempfile
from telebot.apihelper import ApiTelegramException

# ---------------- CONFIG (عدل القيم تحت قبل التشغيل) ----------------
TOKEN = "8554663359:AAH5XjqQuHhzV6MT4K6ccg0HB9odwUCgfsk"  # <-- ضع توكن جديد هنا (لا تشاركه)
OWNER_ID = 8418469217  # ايديك كمالك
BOT_USERNAME = "BDYGOBOT"  # اسم البوت بدون @
DATA_FILE = "botta.json"

# زر "اضغط هنا" يقود لهذه القناة (قناة المقاطع)
SPECIFIC_CHANNEL_USERNAME = "vvhbkklbot"
SPECIFIC_CHANNEL_URL = f"https://t.me/{SPECIFIC_CHANNEL_USERNAME}"
SPECIFIC_CHANNEL_BUTTON_NAME = "قناة المقاطع"

# زر "قناتي" بعد عرض المقاطع
MAIN_CHANNEL_USERNAME = "femboy_IQ"
MAIN_CHANNEL_URL = f"https://t.me/{MAIN_CHANNEL_USERNAME}"
MAIN_CHANNEL_BUTTON_NAME = "قناتي"

# الحد الأقصى للفيديوات في الألبوم
MAX_VIDEOS_PER_GROUP = 10

# مدة حذف الرسائل بعد الإرسال (بالثواني)
TEMP_DELETE_SECONDS = 15

# وقت تبريد استخدام الرابط (ثانية)
LINK_COOLDOWN_SECONDS = 60

# منطق /start سبام
START_SPAM_WARNING_THRESHOLD = 3
START_SPAM_FREEZE_ON = 4
START_FREEZE_SECONDS = 15 * 60  # 15 دقيقة

# حذف بيانات المستخدم كل 5 دقائق (لتخفيف الضغط)
USER_DATA_CLEAN_INTERVAL = 5 * 60

# ---------------- إعداد البوت ----------------
bot = telebot.TeleBot(TOKEN)

# بيانات افتراضية
DEFAULT_DATA = {
    "admins": [OWNER_ID],
    "forced_channels": [],   # {id, username, title}
    "video_groups": {},      # code -> [file_id, ...]
    "users": {},             # "user_id" -> {username, blocked, start_count, last_start_time, start_cooldown_until, last_link_time}
    "temp_messages": {},     # "chat_id" -> [ {"message_id": int, "expire_at": ts}, ... ]
    "broadcast_ids": []
}

data_lock = threading.RLock()

# ---------------- storage utils ----------------
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            # ensure keys
            for k, v in DEFAULT_DATA.items():
                if k not in d:
                    d[k] = v
            return d
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_DATA))

def save_data(data):
    with data_lock:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="botdata_", suffix=".json", dir=".")
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, DATA_FILE)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

bot_data = load_data()

# تأكد من وجود OWNER_ID ضمن المشرفين
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

# ---------------- مستخدمين و temp msgs ----------------
def _store_temp_message(chat_id, message_id, expire_at):
    uid = str(chat_id)
    with data_lock:
        if "temp_messages" not in bot_data:
            bot_data["temp_messages"] = {}
        lst = bot_data["temp_messages"].get(uid, [])
        lst.append({"message_id": message_id, "expire_at": expire_at})
        bot_data["temp_messages"][uid] = lst
        save_data(bot_data)

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
        # حفظ لائحة الإذاعة
        try:
            if "broadcast_ids" not in bot_data:
                bot_data["broadcast_ids"] = []
            if int(user.id) not in [int(x) for x in bot_data["broadcast_ids"]]:
                bot_data["broadcast_ids"].append(int(user.id))
        except Exception:
            pass
        save_data(bot_data)

# ---------------- مفردات (keyboards) ----------------
def subscription_markup(unsubscribed_channels):
    mk = types.InlineKeyboardMarkup()
    for ch in unsubscribed_channels:
        mk.add(types.InlineKeyboardButton(f"اشترك في {ch['title']}", url=ch['link']))
    mk.add(types.InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_sub"))
    return mk

def get_main_keyboard(user_id):
    markup = types.InlineKeyboardMarkup()
    # زر قناة المقاطع الدائم
    markup.add(types.InlineKeyboardButton(SPECIFIC_CHANNEL_BUTTON_NAME, url=SPECIFIC_CHANNEL_URL))
    # أزرار الإدارة للأدمن
    with data_lock:
        admins = bot_data.get("admins", [])
    if int(user_id) in [int(x) for x in admins]:
        markup.add(types.InlineKeyboardButton("➕ إضافة مقاطع", callback_data="admin_add_videos"))
        markup.add(types.InlineKeyboardButton("🗑️ إدارة روابط", callback_data="admin_delete_videos"))
        markup.add(types.InlineKeyboardButton("📢 إرسال إذاعة", callback_data="admin_broadcast"))
        markup.add(types.InlineKeyboardButton("⚙️ إدارة القنوات الإلزامية", callback_data="admin_forced_channels"))
        markup.add(types.InlineKeyboardButton("🔥 تصفير البوت بالكامل", callback_data="admin_wipe_bot"))
    return markup

def get_back_button(callback_data="admin_main_menu"):
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("🔙 رجوع", callback_data=callback_data))
    return mk

# ---------------- مساعدة الاشتراك ----------------
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

# ---------------- حذف مجدول للرسائل ----------------
def delete_messages_after_delay(chat_id, message_ids, delay=TEMP_DELETE_SECONDS):
    def task():
        time.sleep(delay)
        for mid in message_ids:
            try:
                bot.delete_message(chat_id, mid)
            except Exception:
                pass
    threading.Thread(target=task, daemon=True).start()

# ---------------- إرسال الميديا (core) ----------------
def generate_unique_code(length=8):
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        with data_lock:
            if code not in bot_data.get("video_groups", {}):
                return code

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
                    _store_temp_message(chat_id, m.message_id, time.time() + TEMP_DELETE_SECONDS)
                except Exception:
                    pass
            time.sleep(0.2)
        except Exception:
            for fid in video_file_ids:
                try:
                    m = bot.send_video(chat_id, fid, caption="مقطع مؤقت")
                    sent_ids.append(m.message_id)
                    try:
                        _store_temp_message(chat_id, m.message_id, time.time() + TEMP_DELETE_SECONDS)
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
            _store_temp_message(chat_id, final_msg.message_id, time.time() + TEMP_DELETE_SECONDS)
        except Exception:
            pass
    except Exception:
        pass

    # تسجيل الورد وازالة بعد مدة
    delete_messages_after_delay(chat_id, sent_ids, delay=TEMP_DELETE_SECONDS)

    if user_obj:
        uid_str = str(user_obj.id)
        with data_lock:
            udata = bot_data.get("users", {}).get(uid_str, {})
            udata["last_link_time"] = time.time()
            bot_data["users"][uid_str] = udata
            save_data(bot_data)

    return True

# ---------------- رسائل آمنة مقسمة ----------------
MAX_MESSAGE_LENGTH = 4096

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
                _store_temp_message(chat_id, msg.message_id, time.time() + TEMP_DELETE_SECONDS)
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
    return sent_ids

def safe_edit_message_text(chat_id, message_id, text, **kwargs):
    try:
        if text is None:
            return []
        if len(text) < MAX_MESSAGE_LENGTH:
            try:
                bot.edit_message_text(text, chat_id, message_id, **kwargs)
                _store_temp_message(chat_id, message_id, time.time() + TEMP_DELETE_SECONDS)
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

# ---------------- handlers ----------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.chat.type != "private":
        return

    register_user(message.from_user)
    uid_str = str(message.chat.id)
    with data_lock:
        user_data = dict(bot_data.get("users", {}).get(uid_str, {}))
    now = time.time()

    # رابط مشاركة
    if message.text and message.text.startswith("/start _"):
        parts = message.text.split("_", 1)
        if len(parts) < 2:
            safe_send(message.chat.id, "❌ رابط غير صالح.")
            return
        code = parts[1].strip()

        # مضاد سبام للروابط
        last_link = user_data.get("last_link_time", 0)
        if now - last_link < LINK_COOLDOWN_SECONDS:
            safe_send(message.chat.id, "انتظر دقيقة وادخل لرابط المقاطع", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(SPECIFIC_CHANNEL_BUTTON_NAME, url=SPECIFIC_CHANNEL_URL)))
            return

        is_sub, unsub = check_subscription(message.from_user.id)
        if not is_sub:
            with data_lock:
                # خزّن طلب مؤقت
                bot_data.setdefault("pending_codes", {})[message.chat.id] = {"code": code, "requested_at": now}
                save_data(bot_data)
            try:
                bot.send_message(message.chat.id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub))
            except Exception:
                pass
            return

        success = send_media_for_code(message.chat.id, code, user_obj=message.from_user)
        if not success:
            safe_send(message.chat.id, "❌ حدث خطأ أثناء إرسال المقاطع. حاول لاحقاً")
        return

    # معالجة /start العادية (تحذير/تجميد عند تكرار)
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

    if start_count >= START_SPAM_WARNING_THRESHOLD and start_count < START_SPAM_FREEZE_ON:
        safe_send(message.chat.id, '⚠️ ( لا ترسل /start  ، اذا تحتاج مقاطع اذهب لقناة المقاطع واضغط على كلمة " اضغط هنا " 👇)', reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(SPECIFIC_CHANNEL_BUTTON_NAME, url=SPECIFIC_CHANNEL_URL)))
        return
    elif start_count >= START_SPAM_FREEZE_ON:
        user_data["start_cooldown_until"] = now + START_FREEZE_SECONDS
        with data_lock:
            bot_data["users"][uid_str] = user_data
            save_data(bot_data)
        safe_send(message.chat.id, '✅ تم تقييدك من ارسال /start و تجميدك من استخدام البوت لمدة 15 دقيقة. اذا كنت تريد مقاطع ادخل الى هذا القناة', reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(SPECIFIC_CHANNEL_BUTTON_NAME, url=SPECIFIC_CHANNEL_URL)))
        return

    # تحقق الاشتراك للقنوات الاجبارية
    is_sub, unsub_channels = check_subscription(message.chat.id)
    if not is_sub:
        try:
            bot.send_message(message.chat.id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub_channels))
        except Exception:
            pass
        return

    text = 'ادخل للقناة ودوس على " اضغط هنا " و ستظهر لك مقاطع 👇'
    try:
        bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard(message.chat.id))
    except Exception:
        pass

# ---------------- callback: تحقق الاشتراك ----------------
@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_sub_callback(call):
    register_user(call.from_user)
    is_sub, unsub = check_subscription(call.from_user.id)
    if is_sub:
        bot.answer_callback_query(call.id, "✅ تم التحقق، شكراً لاشتراكك!")
        # بعد التحقق نرسل الرابط إن وُجد طلب سابق
        with data_lock:
            pending = bot_data.get("pending_codes", {}).get(call.message.chat.id)
            if pending:
                code = pending.get("code")
                try:
                    del bot_data["pending_codes"][call.message.chat.id]
                except Exception:
                    pass
                save_data(bot_data)
                success = send_media_for_code(call.message.chat.id, code, user_obj=call.from_user)
                if not success:
                    safe_send(call.message.chat.id, "❌ حدث خطأ أثناء إرسال المقاطع بعد التحقق. حاول لاحقاً.")
            else:
                pass
    else:
        bot.answer_callback_query(call.id, "❌ لم يتم الاشتراك بعد. يرجى الاشتراك أولاً.")
        try:
            safe_edit(call.message.chat.id, call.message.message_id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub))
        except Exception:
            try:
                bot.send_message(call.message.chat.id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub))
            except Exception:
                pass

# safe_edit wrapper (بسبب اختلاف التسمية أعلاه)
def safe_edit(chat_id, message_id, text, **kwargs):
    try:
        bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception:
        try:
            bot.send_message(chat_id, text, **kwargs)
        except Exception:
            pass

# ---------------- admin: إحصائيات ----------------
@bot.callback_query_handler(func=lambda c: c.data == "admin_stats")
def admin_stats_callback(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية الوصول.")
        return
    with data_lock:
        total = len(bot_data.get("users", {}))
        blocked = sum(1 for u in bot_data.get("users", {}).values() if u.get("blocked"))
        active = total - blocked
        video_groups_count = len(bot_data.get('video_groups', {}))
        broadcast_count = len(bot_data.get('broadcast_ids', []))
    text = (
        "📊 إحصائيات البوت\n"
        f"👥 إجمالي المستخدمين الذين دخلوا البوت: {total}\n"
        f"✅ المستخدمون النشطون: {active}\n"
        f"🚫 المستخدمون الذين حظروا البوت: {blocked}\n"
        f"🎬 عدد مجموعات المقاطع المخزنة: {video_groups_count}\n"
        f"📮 عدد عناوين البث المحفوظة: {broadcast_count}"
    )
    safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=get_back_button())

# ---------------- admin: broadcast ----------------
admin_temp = {}

@bot.callback_query_handler(func=lambda c: c.data == "admin_broadcast")
def admin_broadcast_callback(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    try:
        msg = bot.edit_message_text("أرسل الآن الرسالة التي تريد إرسالها كإذاعة.\n\nيمكنك إرسال نص، صورة، فيديو، أو أي نوع من الرسائل.", call.message.chat.id, call.message.message_id, reply_markup=get_back_button())
    except Exception:
        try:
            msg = bot.send_message(call.message.chat.id, "أرسل الآن الرسالة التي تريد إرسالها كإذاعة.\n\nيمكنك إرسال نص، صورة، فيديو، أو أي نوع من الرسائل.", reply_markup=get_back_button())
        except Exception:
            return
    admin_temp[call.from_user.id] = {"state": "waiting_broadcast"}

# ---------------- admin: add/delete video groups ----------------
@bot.callback_query_handler(func=lambda c: c.data == "admin_add_videos")
def admin_add_videos_callback(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    try:
        msg = bot.edit_message_text("أرسل الآن عدد الفيديوهات التي سترسلها (مثلاً: 5). الحد الأقصى لكل رابط هو 10.", call.message.chat.id, call.message.message_id, reply_markup=get_back_button())
    except Exception:
        try:
            msg = bot.send_message(call.message.chat.id, "أرسل الآن عدد الفيديوهات التي سترسلها (مثلاً: 5). الحد الأقصى لكل رابط هو 10.", reply_markup=get_back_button())
        except Exception:
            return
    admin_temp[call.from_user.id] = {"state": "waiting_for_count"}

@bot.callback_query_handler(func=lambda c: c.data == "admin_delete_videos")
def admin_delete_videos_callback(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    with data_lock:
        groups = dict(bot_data.get("video_groups", {}))
    if not groups:
        bot.answer_callback_query(call.id, "لا توجد روابط مقاطع مخزنة.")
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

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("view_link_"))
def view_link_callback(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
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

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("disable_link_"))
def disable_link_confirm(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
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

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("confirm_disable_link_"))
def disable_link(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
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

# ---------------- admin: forced channels management ----------------
@bot.callback_query_handler(func=lambda c: c.data == "admin_forced_channels")
def admin_forced_channels_callback(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    forced = bot_data.get("forced_channels", [])
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton('➕ إضافة قناة', callback_data='fc_add'))
    mk.add(types.InlineKeyboardButton('➖ حذف قناة', callback_data='fc_remove'))
    for ch in forced:
        # تم تصحيح هنا: لا توجد باك سلاش داخل f-string
        mk.add(types.InlineKeyboardButton(ch.get('title') or ch.get('username'), callback_data=f"fc_info_{ch.get('id')}"))
    try:
        bot.edit_message_text('إدارة القنوات الإلزامية:', call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        try:
            bot.send_message(call.message.chat.id, 'إدارة القنوات الإلزامية:', reply_markup=mk)
        except Exception:
            pass

# عملية إضافة قناة: سنستخدم تسجيل حالة مبسطة في admin_temp
@bot.callback_query_handler(func=lambda c: c.data == 'fc_add')
def fc_add_start(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    admin_temp[call.from_user.id] = {'state': 'fc_wait_id'}
    try:
        bot.edit_message_text('اجعل البوت أدمن في القناة ثم أرسل ايدي القناة (مثال: -1003675171238). ارسل /cancel لإيقاف.', call.message.chat.id, call.message.message_id)
    except Exception:
        try:
            bot.send_message(call.message.chat.id, 'اجعل البوت أدمن في القناة ثم أرسل ايدي القناة (مثال: -1003675171238). ارسل /cancel لإيقاف.')
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data == 'fc_remove')
def fc_remove_start(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    forced = bot_data.get("forced_channels", [])
    if not forced:
        bot.answer_callback_query(call.id, "لا توجد قنوات مضافة.")
        return
    mk = types.InlineKeyboardMarkup()
    for ch in forced:
        # تم تصحيح هنا أيضاً
        mk.add(types.InlineKeyboardButton(ch.get('title') or ch.get('username'), callback_data=f"fc_del_{ch.get('id')}"))
    try:
        bot.edit_message_text('اختر قناة للحذف:', call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        try:
            bot.send_message(call.message.chat.id, 'اختر قناة للحذف:', reply_markup=mk)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('fc_del_'))
def fc_confirm_delete(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    ch_id = int(call.data.split('_', 2)[2])
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton('نعم', callback_data=f'fc_do_del_{ch_id}'))
    mk.add(types.InlineKeyboardButton('إلغاء', callback_data='admin_forced_channels'))
    try:
        bot.edit_message_text(f'هل تريد المتابعة حذف {ch_id}؟', call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        try:
            bot.send_message(call.message.chat.id, f'هل تريد المتابعة حذف {ch_id}?', reply_markup=mk)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('fc_do_del_'))
def fc_do_delete(call):
    ch_id = int(call.data.split('_', 3)[3])
    with data_lock:
        forced = bot_data.get("forced_channels", [])
        forced = [ch for ch in forced if ch.get("id") != ch_id]
        bot_data["forced_channels"] = forced
        save_data(bot_data)
    bot.answer_callback_query(call.id, "تم الحذف")
    try:
        admin_forced_channels_callback(call)
    except Exception:
        pass

# ---------------- admin: wipe bot ----------------
@bot.callback_query_handler(func=lambda c: c.data == "admin_wipe_bot")
def admin_wipe_bot_confirm(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية")
        return
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("⚠️ نعم، احذف كل شيء", callback_data="confirm_wipe_bot"))
    mk.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="admin_main_menu"))
    try:
        bot.edit_message_text("⚠️ تحذير! سيتم حذف جميع بيانات البوت (باستثناء broadcast_ids). هل أنت متأكد؟", call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        try:
            bot.send_message(call.message.chat.id, "⚠️ تحذير! سيتم حذف جميع بيانات البوت (باستثناء broadcast_ids). هل أنت متأكد؟", reply_markup=mk)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data == "confirm_wipe_bot")
def admin_wipe_bot_execute(call):
    if int(call.from_user.id) not in [int(x) for x in bot_data.get("admins", [])]:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية")
        return
    with data_lock:
        preserved = bot_data.get("broadcast_ids", [])
        bot_data.clear()
        bot_data.update({
            "admins": [OWNER_ID],
            "forced_channels": [],
            "video_groups": {},
            "users": {},
            "temp_messages": {},
            "broadcast_ids": preserved
        })
        save_data(bot_data)
    try:
        bot.send_message(call.message.chat.id, "✅ تم تصفير البوت بنجاح", reply_markup=get_main_keyboard(call.from_user.id))
    except Exception:
        pass

# ---------------- عام: التقاط كل الرسائل للتعامل مع جلسات الإدارة والبث ----------------
@bot.message_handler(func=lambda m: True, content_types=['text','video','photo','document','audio','sticker','voice'])
def handle_all_messages(message):
    # تعامل مع جلسات الادمن (count, videos, forced channel id، broadcast)
    uid = message.from_user.id
    if uid in admin_temp:
        ses = admin_temp[uid]
        state = ses.get("state")
        # إلغاء
        if message.text and message.text.strip().lower() == "/cancel":
            admin_temp.pop(uid, None)
            bot.send_message(message.chat.id, "تم إلغاء العملية.")
            return

        # انتظار عدد فيديوهات
        if state == "waiting_for_count":
            try:
                cnt = int(message.text.strip())
                if cnt <= 0:
                    bot.send_message(message.chat.id, "العدد غير صحيح. أرسل رقم صحيح (1-10).")
                    return
                if cnt > MAX_VIDEOS_PER_GROUP:
                    cnt = MAX_VIDEOS_PER_GROUP
                    bot.send_message(message.chat.id, f"⚠️ تم ضبط العدد إلى الحد الأقصى ({MAX_VIDEOS_PER_GROUP}).")
                admin_temp[uid] = {"state": "waiting_for_videos", "count": cnt, "received": []}
                bot.send_message(message.chat.id, f"أرسل الآن {cnt} فيديو (كمقاطع فيديو في المحادثة).")
            except Exception:
                bot.send_message(message.chat.id, "أرسل عدد صحيح.")
            return

        # استقبال ايدي قناة للإضافة كقناة اجبارية
        if state == "fc_wait_id":
            try:
                ch_id = int(message.text.strip())
            except Exception:
                bot.send_message(message.chat.id, "الرجاء إرسال معرف قناة صالح (رقم يبدأ بـ -100...).")
                admin_temp.pop(uid, None)
                return
            # تحقق وجود البوت كأدمن
            try:
                member = bot.get_chat_member(ch_id, bot.get_me().id)
                if member.status not in ['administrator', 'creator']:
                    bot.send_message(message.chat.id, "البوت ليس أدمن في القناة. اجعله أدمن ثم أعد المحاولة.")
                    admin_temp.pop(uid, None)
                    return
            except Exception:
                bot.send_message(message.chat.id, "تعذر التحقق من القناة. تأكد من صحة المعرف وأن البوت موجود فيها.")
                admin_temp.pop(uid, None)
                return
            try:
                ch = bot.get_chat(ch_id)
                with data_lock:
                    forced = bot_data.get("forced_channels", [])
                    forced.append({"id": ch_id, "username": (ch.username or "").lstrip('@'), "title": ch.title or ch.username or str(ch_id)})
                    bot_data["forced_channels"] = forced
                    save_data(bot_data)
                bot.send_message(message.chat.id, f"✅ تم إضافة {ch.title} إلى القنوات الإلزامية.")
            except Exception:
                bot.send_message(message.chat.id, "حدث خطأ أثناء الإضافة.")
            admin_temp.pop(uid, None)
            return

        # استقبال بث (انتظار رسالة اذاعة)
        if state == "waiting_broadcast":
            # جمع قوائم البث
            with data_lock:
                b_ids = list(bot_data.get("broadcast_ids", []))
            success = 0
            fail = 0
            for u in b_ids:
                try:
                    bot.copy_message(u, message.chat.id, message.message_id)
                    success += 1
                    time.sleep(0.03)
                except Exception:
                    fail += 1
            bot.send_message(message.chat.id, f"✅ انتهت الإذاعة! تم الإرسال بنجاح إلى: {success} عنوان. فشل إلى: {fail} عنوان.")
            admin_temp.pop(uid, None)
            return

    # استقبال ملفات الفيديو أثناء جلسة رفع الفيديوات للأدمن
    if message.content_type == 'video' and uid in admin_temp:
        ses = admin_temp.get(uid)
        if ses and ses.get("state") == "waiting_for_videos":
            fid = message.video.file_id
            ses["received"].append(fid)
            remaining = ses["count"] - len(ses["received"])
            if remaining > 0:
                bot.send_message(message.chat.id, f"✅ استلمت مقطع. المتبقي: {remaining}")
            else:
                # حفظ المجموعة
                code = generate_unique_code()
                with data_lock:
                    bot_data.setdefault("video_groups", {})[code] = ses["received"][:MAX_VIDEOS_PER_GROUP]
                    save_data(bot_data)
                share_link = f"https://t.me/{BOT_USERNAME}?start=_{code}"
                bot.send_message(message.chat.id, f"🎉 تم استلام جميع المقاطع!\n\nرابط المشاركة:\n`{share_link}`", parse_mode="Markdown", reply_markup=get_main_keyboard(message.chat.id))
                admin_temp.pop(uid, None)
            return

    # غير ذلك: نرسل لوحة الأزرار الرئيسية
    if message.chat.type == "private":
        register_user(message.from_user)
        bot.send_message(message.chat.id, "الرجاء استخدام الأزرار أدناه:", reply_markup=get_main_keyboard(message.chat.id))

# ---------------- حلقة تنظيف الرسائل من ملف البيانات ----------------
def background_cleanup_loop():
    while True:
        try:
            now = time.time()
            with data_lock:
                data = load_data()
                temp = data.get("temp_messages", {})
                changed = False
                for uid, items in list(temp.items()):
                    remaining = []
                    for it in items:
                        if it.get("expire_at", 0) <= now:
                            try:
                                bot.delete_message(int(uid), int(it.get("message_id")))
                            except Exception:
                                pass
                            changed = True
                        else:
                            remaining.append(it)
                    if remaining:
                        temp[uid] = remaining
                    else:
                        if uid in temp:
                            del temp[uid]
                data["temp_messages"] = temp
                if changed:
                    save_data(data)
        except Exception:
            pass
        time.sleep(3)

# ---------------- حلقة تنظيف بيانات المستخدم كل 5 دقائق ----------------
def periodic_user_cleanup():
    while True:
        try:
            with data_lock:
                data = load_data()
                users = data.get("users", {})
                for uid, u in list(users.items()):
                    keep = {'username': u.get('username', 'N/A')}
                    users[uid] = keep
                data['users'] = users
                save_data(data)
        except Exception:
            pass
        time.sleep(USER_DATA_CLEAN_INTERVAL)

# ---------------- بدء الخيوط الخلفية ----------------
cleanup_thread = threading.Thread(target=background_cleanup_loop, daemon=True)
cleanup_thread.start()

user_clean_thread = threading.Thread(target=periodic_user_cleanup, daemon=True)
user_clean_thread.start()

# ---------------- تشغيل البوت ----------------
if __name__ == "__main__":
    print("Bot is starting... تأكد أنك عدّلت TOKEN في أعلى الملف")
    try:
        bot.polling(none_stop=True)
    except Exception as e:
        print("Polling error:", e)
        time.sleep(5)
        bot.polling(none_stop=True)