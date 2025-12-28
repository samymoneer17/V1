# -*- coding: utf-8 -*-
"""
Bot: إرسال ألبومات فيديو (حتى 10 فيديو) من قناة خاصة -> للمستخدم ثم حذف بعد 15s.
Flow:
- Admin (OWNER_ID) يضغط إنشاء رابط: يرسل عدد الفيديوهات ثم يرسل الفيديوهات للبوت.
- البوت ينشر الألبوم في القناة الخاصة (CHANNEL_ID) ويخزن file_ids كـ رابط (code).
- المستخدم يضغط الرابط (start _CODE) -> البوت يتحقق من الاشتراك (forced_channels.json) -> يرسل الألبوم للمستخدم -> يحذف كل الرسائل بعد 15s.
Files used:
- bot_data.json (users, temp_messages, pending_codes, broadcast_ids, video_groups minimal)
- forced_channels.json (قنوات اجبارية ثابتة)
- links.json (رابط -> {channel_id, file_ids, created_at, disabled})
"""
import os
import json
import time
import threading
import tempfile
import random
import string
import traceback

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

# ---------------- CONFIG ----------------
TOKEN = "8554663359:AAFkgn5Ih0g8mtsQtIfdODynYo_YwDFXAC8"  # <- ضع التوكن الجديد هنا
OWNER_ID = 8418469217
BOT_USERNAME = "BDYGOBOT"  # بدون @
CHANNEL_ID = -1003622339628  # قناتك الخاصة (أنت أعطيت هذا)
DATA_FILE = "bobt_data.json"
FORCED_FILE = "forcebbd_channels.json"
LINKS_FILE = "linkbbs.json"

# نصوص وأزرار
SPECIFIC_CHANNEL_BUTTON_NAME = "قناة المقاطع"
SPECIFIC_CHANNEL_URL = "https://t.me/vvhbkklbot"
MAIN_CHANNEL_BUTTON_NAME = "قناتي"
MAIN_CHANNEL_URL = "https://t.me/femboy_IQ"

# حدود ووقت
MAX_VIDEOS_PER_GROUP = 10
TEMP_DELETE_SECONDS = 15
LINK_COOLDOWN_SECONDS = 60
START_SPAM_WARNING_THRESHOLD = 3
START_SPAM_FREEZE_ON = 4
START_FREEZE_SECONDS = 15 * 60
USER_DATA_CLEAN_INTERVAL = 5 * 60
CLEANUP_CHECK_INTERVAL = 3

# ---------------- locks ----------------
data_lock = threading.RLock()
links_lock = threading.RLock()
forced_lock = threading.RLock()

# ---------------- defaults ----------------
DEFAULT_DATA = {
    "users": {},            # "uid" -> {username, start_count, last_start_time, start_cooldown_until, last_link_time, blocked}
    "temp_messages": {},    # "chat_id" -> [ {"message_id": int, "expire_at": ts}, ... ]
    "pending_codes": {},    # chat_id -> {"code": code, "requested_at": ts}
    "broadcast_ids": []     # list of user ids
}

# ---------------- bot init ----------------
bot = telebot.TeleBot(TOKEN)

# ---------------- file utils ----------------
def atomic_load(path, default):
    try:
        if not os.path.exists(path):
            return json.loads(json.dumps(default))
        with open(path, 'r', encoding='utf-8') as f:
            txt = f.read()
            if not txt:
                return json.loads(json.dumps(default))
            return json.loads(txt)
    except Exception:
        print(f"[WARN] failed load {path}:\n{traceback.format_exc()}")
        return json.loads(json.dumps(default))

def atomic_save(path, data):
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="tmp_", suffix=".json", dir=".")
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        print(f"[ERROR] failed save {path}: {e}")
        return False

# ---------------- data management ----------------
def load_bot_data():
    with data_lock:
        data = atomic_load(DATA_FILE, DEFAULT_DATA)
        # ensure keys
        for k, v in DEFAULT_DATA.items():
            if k not in data:
                data[k] = v
        return data

def save_bot_data(data):
    with data_lock:
        return atomic_save(DATA_FILE, data)

# links file: code -> {channel_id, file_ids, created_at, disabled}
def load_links():
    with links_lock:
        return atomic_load(LINKS_FILE, {})

def save_links(d):
    with links_lock:
        return atomic_save(LINKS_FILE, d)

# forced channels file
def load_forced_channels():
    with forced_lock:
        return atomic_load(FORCED_FILE, {"channels": []})

def save_forced_channels(d):
    with forced_lock:
        return atomic_save(FORCED_FILE, d)

# ---------------- helpers ----------------
def generate_code(length=10):
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        links = load_links()
        if code not in links:
            return code

def _store_temp_message(chat_id, message_id, expire_at):
    uid = str(chat_id)
    with data_lock:
        data = load_bot_data()
        temp = data.setdefault("temp_messages", {})
        lst = temp.get(uid, [])
        lst.append({"message_id": int(message_id), "expire_at": int(expire_at)})
        temp[uid] = lst
        data["temp_messages"] = temp
        save_bot_data(data)

def register_user(user):
    uid = str(user.id)
    with data_lock:
        data = load_bot_data()
        users = data.setdefault("users", {})
        if uid not in users:
            users[uid] = {
                "username": user.username if user.username else "N/A",
                "start_count": 0,
                "last_start_time": 0,
                "start_cooldown_until": 0,
                "last_link_time": 0,
                "blocked": False
            }
        # add to broadcast list
        b = data.setdefault("broadcast_ids", [])
        if int(user.id) not in [int(x) for x in b]:
            b.append(int(user.id))
            data["broadcast_ids"] = b
        data["users"] = users
        save_bot_data(data)

def get_main_keyboard(user_id):
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton(SPECIFIC_CHANNEL_BUTTON_NAME, url=SPECIFIC_CHANNEL_URL))
    data = load_bot_data()
    admins = [OWNER_ID]  # only owner for simplicity
    if int(user_id) in admins:
        mk.add(types.InlineKeyboardButton("➕ إنشاء رابط", callback_data="admin_add_videos"))
        mk.add(types.InlineKeyboardButton("🗑️ إدارة روابط", callback_data="admin_manage_links"))
        mk.add(types.InlineKeyboardButton("⚙️ القنوات الإجبارية", callback_data="admin_forced_channels"))
        mk.add(types.InlineKeyboardButton("📢 الإذاعة", callback_data="admin_broadcast"))
        mk.add(types.InlineKeyboardButton("🔥 تصفير البوت", callback_data="admin_wipe_bot"))
    return mk

def subscription_markup(unsub):
    mk = types.InlineKeyboardMarkup()
    for ch in unsub:
        mk.add(types.InlineKeyboardButton(f"اشترك في {ch.get('title')}", url=ch.get('link')))
    mk.add(types.InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_sub"))
    return mk

# ---------------- subscription check ----------------
def check_subscription_sync(user_id):
    fc = load_forced_channels().get("channels", [])
    if not fc:
        return True, []
    unsub = []
    for ch in fc:
        uname = ch.get("username")
        title = ch.get("title") or (uname or str(ch.get("id")))
        if uname:
            try:
                member = bot.get_chat_member(f"@{uname}", user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    unsub.append({"title": title, "link": f"https://t.me/{uname}"})
            except Exception:
                unsub.append({"title": title, "link": f"https://t.me/{uname}"})
        else:
            try:
                member = bot.get_chat_member(ch.get("id"), user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    link = ch.get("invite_link") or f"https://t.me/{ch.get('id')}"
                    unsub.append({"title": title, "link": link})
            except Exception:
                link = ch.get("invite_link") or f"https://t.me/{ch.get('id')}"
                unsub.append({"title": title, "link": link})
    return (len(unsub) == 0), unsub

# ---------------- send album and schedule delete ----------------
def send_album_to_user_and_expire(chat_id, file_ids, user_obj=None):
    # send as media group (album)
    sent_ids = []
    try:
        media = [types.InputMediaVideo(fid) for fid in file_ids[:MAX_VIDEOS_PER_GROUP]]
        messages = bot.send_media_group(chat_id, media)
        for m in messages:
            sent_ids.append(m.message_id)
            _store_temp_message(chat_id, m.message_id, time.time() + TEMP_DELETE_SECONDS)
            time.sleep(0.03)
    except Exception:
        # fallback individual send (rare)
        for fid in file_ids[:MAX_VIDEOS_PER_GROUP]:
            try:
                m = bot.send_video(chat_id, fid)
                sent_ids.append(m.message_id)
                _store_temp_message(chat_id, m.message_id, time.time() + TEMP_DELETE_SECONDS)
                time.sleep(0.03)
            except Exception:
                pass
    # final message
    try:
        mk = types.InlineKeyboardMarkup()
        mk.add(types.InlineKeyboardButton(MAIN_CHANNEL_BUTTON_NAME, url=MAIN_CHANNEL_URL))
        final = bot.send_message(chat_id, "شبيك صافن؟ حول مقاطع بسرعة قبل لا ينحذفن بعد 15 ثانية!!", reply_markup=mk)
        sent_ids.append(final.message_id)
        _store_temp_message(chat_id, final.message_id, time.time() + TEMP_DELETE_SECONDS)
    except Exception:
        pass

    # background delete (also cleanup loop will handle if restart)
    threading.Thread(target=lambda: delete_messages_after_delay(chat_id, sent_ids, TEMP_DELETE_SECONDS), daemon=True).start()

    # update user last_link_time
    if user_obj:
        uid_str = str(user_obj.id)
        with data_lock:
            data = load_bot_data()
            users = data.setdefault("users", {})
            u = users.get(uid_str, {})
            u["last_link_time"] = time.time()
            users[uid_str] = u
            data["users"] = users
            save_bot_data(data)
    return True

def delete_messages_after_delay(chat_id, message_ids, delay=TEMP_DELETE_SECONDS):
    time.sleep(delay)
    for mid in message_ids:
        try:
            bot.delete_message(chat_id, mid)
        except Exception:
            pass
    # remove from temp_messages stored
    with data_lock:
        data = load_bot_data()
        temp = data.get("temp_messages", {})
        uid = str(chat_id)
        if uid in temp:
            remaining = [it for it in temp[uid] if it.get("message_id") not in message_ids]
            if remaining:
                temp[uid] = remaining
            else:
                del temp[uid]
            data["temp_messages"] = temp
            save_bot_data(data)

# ---------------- safe send/edit ----------------
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
            _store_temp_message(chat_id, msg.message_id, time.time() + TEMP_DELETE_SECONDS)
        except ApiTelegramException as e:
            err = str(e).lower()
            if "bot was blocked" in err or "forbidden" in err:
                with data_lock:
                    data = load_bot_data()
                    u = data.get("users", {}).get(str(chat_id), {})
                    if u:
                        u["blocked"] = True
                        data["users"][str(chat_id)] = u
                        save_bot_data(data)
            else:
                print(f"[WARN] safe_send failed: {e}")
        except Exception:
            print("[WARN] safe_send error:", traceback.format_exc())
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
            except ApiTelegramException:
                pass
    except Exception:
        pass
    return safe_send(chat_id, text, **kwargs)

# ---------------- handlers ----------------
@bot.message_handler(commands=['start'])
def handle_start(message):
    if message.chat.type != "private":
        return
    register_user(message.from_user)
    uid_str = str(message.chat.id)
    with data_lock:
        data = load_bot_data()
        user_data = dict(data.get("users", {}).get(uid_str, {}))
    now = time.time()

    # if link start: /start _CODE
    if message.text and message.text.startswith("/start _"):
        parts = message.text.split("_", 1)
        if len(parts) < 2:
            safe_send(message.chat.id, "❌ رابط غير صالح.")
            return
        code = parts[1].strip()
        links = load_links()
        item = links.get(code)
        if not item or item.get("disabled"):
            safe_send(message.chat.id, "❌ عذراً، رابط المقاطع هذا غير صالح أو تم تعطيله.")
            return

        # link cooldown
        last_link = user_data.get("last_link_time", 0)
        if now - last_link < LINK_COOLDOWN_SECONDS:
            safe_send(message.chat.id, "انتظر دقيقة وادخل لرابط المقاطع", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(SPECIFIC_CHANNEL_BUTTON_NAME, url=SPECIFIC_CHANNEL_URL)))
            return

        # subscription check
        is_sub, unsub = check_subscription_sync(message.from_user.id)
        if not is_sub:
            # store pending
            with data_lock:
                data = load_bot_data()
                pend = data.setdefault("pending_codes", {})
                pend[message.chat.id] = {"code": code, "requested_at": now}
                data["pending_codes"] = pend
                save_bot_data(data)
            try:
                bot.send_message(message.chat.id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub))
            except Exception:
                pass
            return

        # send album to user using stored file_ids
        file_ids = item.get("file_ids", [])[:MAX_VIDEOS_PER_GROUP]
        if not file_ids:
            safe_send(message.chat.id, "❌ لا يوجد مقاطع لهذا الرابط.")
            return
        send_album_to_user_and_expire(message.chat.id, file_ids, user_obj=message.from_user)
        return

    # normal /start (spam protections)
    last_start = user_data.get("last_start_time", 0)
    start_count = user_data.get("start_count", 0)
    if now - last_start < 1:
        start_count += 1
    else:
        start_count = 1
    user_data["last_start_time"] = now
    user_data["start_count"] = start_count
    with data_lock:
        data = load_bot_data()
        data.setdefault("users", {})[uid_str] = user_data
        save_bot_data(data)

    if start_count >= START_SPAM_WARNING_THRESHOLD and start_count < START_SPAM_FREEZE_ON:
        safe_send(message.chat.id, '⚠️ ( لا ترسل /start  ، اذا تحتاج مقاطع اذهب لقناة المقاطع واضغط على كلمة " اضغط هنا " 👇)', reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(SPECIFIC_CHANNEL_BUTTON_NAME, url=SPECIFIC_CHANNEL_URL)))
        return
    elif start_count >= START_SPAM_FREEZE_ON:
        user_data["start_cooldown_until"] = now + START_FREEZE_SECONDS
        with data_lock:
            data = load_bot_data()
            data.setdefault("users", {})[uid_str] = user_data
            save_bot_data(data)
        safe_send(message.chat.id, '✅ تم تقييدك من ارسال /start و تجميدك من استخدام البوت لمدة 15 دقيقة. اذا كنت تريد مقاطع ادخل الى هذا القناة', reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(SPECIFIC_CHANNEL_BUTTON_NAME, url=SPECIFIC_CHANNEL_URL)))
        return

    # subscription check
    is_sub, unsub = check_subscription_sync(message.from_user.id)
    if not is_sub:
        try:
            bot.send_message(message.chat.id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub))
        except Exception:
            pass
        return

    # welcome
    try:
        bot.send_message(message.chat.id, 'ادخل للقناة ودوس على " اضغط هنا " و ستظهر لك مقاطع 👇', reply_markup=get_main_keyboard(message.chat.id))
    except Exception:
        pass

@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def callback_check_sub(call):
    register_user(call.from_user)
    is_sub, unsub = check_subscription_sync(call.from_user.id)
    if is_sub:
        bot.answer_callback_query(call.id, "✅ تم التحقق، شكراً لاشتراكك!")
        # pending code send if exists
        with data_lock:
            data = load_bot_data()
            pend = data.get("pending_codes", {})
            pending = pend.get(call.message.chat.id)
            if pending:
                code = pending.get("code")
                try:
                    del pend[call.message.chat.id]
                except Exception:
                    pass
                data["pending_codes"] = pend
                save_bot_data(data)
                item = load_links().get(code)
                if item:
                    file_ids = item.get("file_ids", [])[:MAX_VIDEOS_PER_GROUP]
                    if file_ids:
                        send_album_to_user_and_expire(call.message.chat.id, file_ids, user_obj=call.from_user)
    else:
        bot.answer_callback_query(call.id, "❌ لم يتم الاشتراك بعد. يرجى الاشتراك أولاً.")
        try:
            safe_edit_message_text(call.message.chat.id, call.message.message_id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub))
        except Exception:
            try:
                bot.send_message(call.message.chat.id, "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=subscription_markup(unsub))
            except Exception:
                pass

# ---------------- admin flows (OWNER only) ----------------
ADMIN_SESSIONS = {}  # owner_id -> session state

@bot.callback_query_handler(func=lambda c: c.data == "admin_add_videos")
def cb_admin_add_videos(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    try:
        bot.edit_message_text("أرسل الآن عدد الفيديوهات التي سترسلها (1-10). ارسل /cancel لإلغاء.", call.message.chat.id, call.message.message_id, reply_markup=get_main_keyboard(call.from_user.id))
    except Exception:
        bot.send_message(call.message.chat.id, "أرسل الآن عدد الفيديوهات التي سترسلها (1-10). ارسل /cancel لإلغاء.", reply_markup=get_main_keyboard(call.from_user.id))
    ADMIN_SESSIONS[call.from_user.id] = {"state": "waiting_count"}

@bot.callback_query_handler(func=lambda c: c.data == "admin_manage_links")
def cb_admin_manage_links(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    links = load_links()
    if not links:
        bot.answer_callback_query(call.id, "لا توجد روابط.")
        return
    mk = types.InlineKeyboardMarkup()
    for code, item in links.items():
        mk.row(types.InlineKeyboardButton(f"عرض ({len(item.get('file_ids', []))} مقطع)", callback_data=f"view_{code}"),
               types.InlineKeyboardButton("تعطيل", callback_data=f"disable_{code}"))
    mk.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="admin_main"))
    try:
        bot.edit_message_text("اختر رابط لإدارته:", call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        bot.send_message(call.message.chat.id, "اختر رابط لإدارته:", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("view_"))
def cb_view_link(call):
    code = call.data.split("_", 1)[1]
    links = load_links()
    item = links.get(code)
    if not item:
        bot.answer_callback_query(call.id, "الرابط غير موجود.")
        return
    share_link = f"https://t.me/{BOT_USERNAME}?start=_{code}"
    safe_send(call.message.chat.id, f"رابط المقاطع:\n`{share_link}`", parse_mode="Markdown")
    bot.answer_callback_query(call.id, "تم إرسال الرابط.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("disable_"))
def cb_disable_link(call):
    code = call.data.split("_", 1)[1]
    links = load_links()
    if code not in links:
        bot.answer_callback_query(call.id, "غير موجود.")
        return
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton(f"✅ تأكيد تعطيل {code}", callback_data=f"confirm_disable_{code}"))
    mk.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="admin_manage_links"))
    try:
        bot.edit_message_text(f"هل أنت متأكد من تعطيل الرابط {code}؟", call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        bot.send_message(call.message.chat.id, f"هل أنت متأكد من تعطيل الرابط {code}؟", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("confirm_disable_"))
def cb_confirm_disable(call):
    code = call.data.split("_", 2)[2]
    with links_lock:
        links = load_links()
        if code in links:
            links[code]["disabled"] = True
            save_links(links)
            bot.answer_callback_query(call.id, f"✅ تم تعطيل {code}.")
        else:
            bot.answer_callback_query(call.id, "لم أجد الرابط.")
    try:
        cb_admin_manage_links(call)
    except Exception:
        pass

# forced channels admin
@bot.callback_query_handler(func=lambda c: c.data == "admin_forced_channels")
def cb_admin_forced_channels(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    data = load_forced_channels()
    channels = data.get("channels", [])
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("➕ إضافة قناة", callback_data="fc_add"))
    mk.add(types.InlineKeyboardButton("➖ حذف قناة", callback_data="fc_remove"))
    for ch in channels:
        title = ch.get("title") or ch.get("username") or str(ch.get("id"))
        mk.add(types.InlineKeyboardButton(title, callback_data=f"fc_info_{ch.get('id')}"))
    try:
        bot.edit_message_text("إدارة القنوات الإجبارية:", call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        bot.send_message(call.message.chat.id, "إدارة القنوات الإجبارية:", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data == "fc_add")
def cb_fc_add(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    ADMIN_SESSIONS[call.from_user.id] = {"state": "fc_wait_id"}
    try:
        bot.edit_message_text("اجعل البوت أدمن في القناة ثم أرسل ايدي القناة (مثال: -1003675171238). ارسل /cancel لإيقاف.", call.message.chat.id, call.message.message_id)
    except Exception:
        bot.send_message(call.message.chat.id, "اجعل البوت أدمن في القناة ثم أرسل ايدي القناة (مثال: -1003675171238). ارسل /cancel لإيقاف.")

@bot.callback_query_handler(func=lambda c: c.data == "fc_remove")
def cb_fc_remove(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    data = load_forced_channels()
    channels = data.get("channels", [])
    if not channels:
        bot.answer_callback_query(call.id, "لا توجد قنوات مضافة.")
        return
    mk = types.InlineKeyboardMarkup()
    for ch in channels:
        title = ch.get("title") or ch.get("username") or str(ch.get("id"))
        mk.add(types.InlineKeyboardButton(title, callback_data=f"fc_del_{ch.get('id')}"))
    try:
        bot.edit_message_text("اختر قناة للحذف:", call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        bot.send_message(call.message.chat.id, "اختر قناة للحذف:", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("fc_del_"))
def cb_fc_del_confirm(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    ch_id = int(call.data.split("_", 2)[2])
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("نعم", callback_data=f"fc_do_del_{ch_id}"))
    mk.add(types.InlineKeyboardButton("إلغاء", callback_data="admin_forced_channels"))
    try:
        bot.edit_message_text(f"هل تريد المتابعة حذف {ch_id}؟", call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        bot.send_message(call.message.chat.id, f"هل تريد المتابعة حذف {ch_id}؟", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("fc_do_del_"))
def cb_fc_do_delete(call):
    ch_id = int(call.data.split("_", 3)[3])
    data = load_forced_channels()
    data["channels"] = [c for c in data.get("channels", []) if c.get("id") != ch_id]
    save_forced_channels(data)
    bot.answer_callback_query(call.id, "تم الحذف")
    try:
        cb_admin_forced_channels(call)
    except Exception:
        pass

# admin broadcast & wipe
@bot.callback_query_handler(func=lambda c: c.data == "admin_broadcast")
def cb_admin_broadcast(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    ADMIN_SESSIONS[call.from_user.id] = {"state": "waiting_broadcast"}
    try:
        bot.edit_message_text("أرسل الآن الرسالة التي تريد إرسالها كإذاعة.", call.message.chat.id, call.message.message_id, reply_markup=get_main_keyboard(call.from_user.id))
    except Exception:
        bot.send_message(call.message.chat.id, "أرسل الآن الرسالة التي تريد إرسالها كإذاعة.", reply_markup=get_main_keyboard(call.from_user.id))

@bot.callback_query_handler(func=lambda c: c.data == "admin_wipe_bot")
def cb_admin_wipe(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("⚠️ نعم، احذف كل شيء (غير شامل forced_channels.json)", callback_data="confirm_wipe"))
    mk.add(types.InlineKeyboardButton("إلغاء", callback_data="admin_main"))
    try:
        bot.edit_message_text("⚠️ سيتم حذف بيانات البوت (باستثناء forced_channels.json). هل أنت متأكد؟", call.message.chat.id, call.message.message_id, reply_markup=mk)
    except Exception:
        bot.send_message(call.message.chat.id, "⚠️ سيتم حذف بيانات البوت (باستثناء forced_channels.json). هل أنت متأكد؟", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data == "confirm_wipe")
def cb_confirm_wipe(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية.")
        return
    with data_lock:
        data = load_bot_data()
        preserved = data.get("broadcast_ids", [])
        data.clear()
        data.update({
            "users": {},
            "temp_messages": {},
            "pending_codes": {},
            "broadcast_ids": preserved
        })
        save_bot_data(data)
    bot.answer_callback_query(call.id, "✅ تم تصفير البوت (forced_channels محفوظة).")
    try:
        bot.send_message(call.message.chat.id, "✅ تم تصفير البوت بنجاح. القنوات الإجبارية محفوظة في forced_channels.json.")
    except Exception:
        pass

# ---------------- message handler for admin sessions (OWNER) ----------------
@bot.message_handler(func=lambda m: True, content_types=['text','video','photo','document','audio','sticker','voice'])
def handle_all(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    # admin session active?
    if uid in ADMIN_SESSIONS:
        ses = ADMIN_SESSIONS[uid]
        state = ses.get("state")

        # cancel
        if text.lower() == "/cancel":
            ADMIN_SESSIONS.pop(uid, None)
            bot.send_message(message.chat.id, "تم إلغاء العملية.")
            return

        # waiting_count
        if state == "waiting_count":
            if text.isdigit():
                cnt = int(text)
                if cnt <= 0:
                    bot.send_message(message.chat.id, "العدد غير صحيح. ارسل رقم 1-10.")
                    return
                if cnt > MAX_VIDEOS_PER_GROUP:
                    cnt = MAX_VIDEOS_PER_GROUP
                    bot.send_message(message.chat.id, f"⚠️ تم ضبط العدد إلى الحد الأقصى ({MAX_VIDEOS_PER_GROUP}).")
                ADMIN_SESSIONS[uid] = {"state": "waiting_videos", "count": cnt, "received": []}
                bot.send_message(message.chat.id, f"الآن أرسل {cnt} فيديو (كمرفقات فيديو).")
            else:
                bot.send_message(message.chat.id, "أرسل عدد صحيح (1-10) أو /cancel.")
            return

        # waiting_videos
        if state == "waiting_videos":
            # accept video messages
            if message.content_type == "video":
                fid = message.video.file_id
                ses["received"].append(fid)
                remaining = ses["count"] - len(ses["received"])
                if remaining > 0:
                    bot.send_message(message.chat.id, f"✅ استلمت مقطع. المتبقي: {remaining}")
                else:
                    # publish to channel (CHANNEL_ID) as album
                    file_ids = ses["received"][:MAX_VIDEOS_PER_GROUP]
                    try:
                        medias = [types.InputMediaVideo(fid) for fid in file_ids]
                        msgs = bot.send_media_group(CHANNEL_ID, medias)
                        # record file_ids (we already have them) and save link
                        code = generate_code(10)
                        links = load_links()
                        links[code] = {
                            "channel_id": CHANNEL_ID,
                            "file_ids": file_ids,
                            "created_at": int(time.time()),
                            "disabled": False
                        }
                        save_links(links)
                        share_link = f"https://t.me/{BOT_USERNAME}?start=_{code}"
                        bot.send_message(message.chat.id, f"🎉 تم نشر الألبوم في القناة!\n\nرابط المشاركة:\n`{share_link}`", parse_mode="Markdown", reply_markup=get_main_keyboard(message.chat.id))
                    except Exception:
                        bot.send_message(message.chat.id, "حدث خطأ أثناء نشر الألبوم في القناة.")
                    ADMIN_SESSIONS.pop(uid, None)
            else:
                bot.send_message(message.chat.id, "أرسل ملفات فيديو فقط أثناء جلسة رفع المقاطع.")
            return

        # fc_wait_id etc can be handled similarly if ADMIN_SESSIONS used for forced channels
        if state == "fc_wait_id":
            # expecting a channel id to add to forced list
            try:
                ch_id = int(text)
            except Exception:
                bot.send_message(message.chat.id, "الرجاء إرسال معرف قناة صالح (مثال: -1003675171238).")
                ADMIN_SESSIONS.pop(uid, None)
                return
            # verify bot admin
            try:
                me = bot.get_me()
                member = bot.get_chat_member(ch_id, me.id)
                if member.status not in ["administrator", "creator"]:
                    bot.send_message(message.chat.id, "البوت ليس أدمن في القناة. اجعله أدمن ثم أعد المحاولة.")
                    ADMIN_SESSIONS.pop(uid, None)
                    return
            except Exception:
                bot.send_message(message.chat.id, "تعذر التحقق من القناة. تأكد من صحة المعرف وأن البوت موجود فيها.")
                ADMIN_SESSIONS.pop(uid, None)
                return
            try:
                ch = bot.get_chat(ch_id)
                with forced_lock:
                    data = load_forced_channels()
                    data.setdefault("channels", []).append({
                        "id": ch_id,
                        "title": ch.title or ch.username or str(ch_id),
                        "username": (ch.username or "").lstrip('@'),
                        "invite_link": None
                    })
                    save_forced_channels(data)
                bot.send_message(message.chat.id, f"✅ تم إضافة {ch.title} إلى القنوات الإجبارية.")
            except Exception:
                bot.send_message(message.chat.id, "حدث خطأ أثناء الإضافة.")
            ADMIN_SESSIONS.pop(uid, None)
            return

    # admin upload handled above; normal users:
    if message.chat.type == "private":
        register_user(message.from_user)
        bot.send_message(message.chat.id, "الرجاء استخدام الأزرار أدناه:", reply_markup=get_main_keyboard(message.chat.id))

# ---------------- background cleanup loops ----------------
def cleanup_expired_messages_loop():
    while True:
        try:
            now = int(time.time())
            with data_lock:
                data = load_bot_data()
                temp = data.get("temp_messages", {})
                changed = False
                for uid, items in list(temp.items()):
                    remaining = []
                    for it in items:
                        if int(it.get("expire_at", 0)) <= now:
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
                    save_bot_data(data)
        except Exception:
            print("[WARN] cleanup loop error:", traceback.format_exc())
        time.sleep(CLEANUP_CHECK_INTERVAL)

def periodic_user_trim_loop():
    while True:
        try:
            with data_lock:
                data = load_bot_data()
                users = data.get("users", {})
                for uid, u in list(users.items()):
                    users[uid] = {"username": u.get("username", "N/A")}
                data["users"] = users
                save_bot_data(data)
        except Exception:
            print("[WARN] periodic trim error:", traceback.format_exc())
        time.sleep(USER_DATA_CLEAN_INTERVAL)

cleanup_thread = threading.Thread(target=cleanup_expired_messages_loop, daemon=True)
cleanup_thread.start()

trim_thread = threading.Thread(target=periodic_user_trim_loop, daemon=True)
trim_thread.start()

# ---------------- run bot ----------------
if __name__ == "__main__":
    print("Bot starting... تأكد من وضع توكن جديد في TOKEN و BOT_USERNAME صحيح")
    try:
        bot.polling(none_stop=True)
    except Exception as e:
        print("Polling error:", e)
        time.sleep(5)
        try:
            bot.polling(none_stop=True)
        except Exception as e2:
            print("Second polling attempt failed:", e2)