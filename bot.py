import os
import re
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
    LinkPreviewOptions,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG & DATABASE
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
KYIV = ZoneInfo("Europe/Kyiv")

# Оновлені налаштування каналів
CHANNELS = {
    -1004294187385: "Тест",
    -1001509352451: "🇷🇴 Українці у Румунії",
}

posts = {}
user_states = {}  # Зберігає стан користувача

MONTHS_UA = {
    1: "січ", 2: "лют", 3: "бер", 4: "кві", 5: "трав", 6: "черв",
    7: "лип", 8: "серп", 9: "верес", 10: "жовт", 11: "листоп", 12: "груд"
}

DAYS_UA = {
    0: "пн", 1: "вт", 2: "ср", 3: "чт", 4: "пт", 5: "сб", 6: "нд"
}

def init_db():
    conn = sqlite3.connect("posts.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            post_id TEXT PRIMARY KEY,
            user_id INTEGER,
            channel_id INTEGER,
            text TEXT,
            preview_url TEXT,
            photo_file_id TEXT,
            video_file_id TEXT,
            publish_time TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# HELPERS
# =========================================================

def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2

def copy_entities(entities):
    if not entities:
        return []
    result = []
    for entity in entities:
        result.append(
            MessageEntity(
                type=entity.type,
                offset=entity.offset,
                length=entity.length,
                url=getattr(entity, "url", None),
                user=getattr(entity, "user", None),
                language=getattr(entity, "language", None),
                custom_emoji_id=getattr(entity, "custom_emoji_id", None),
            )
        )
    return result

def extract_urls(text, entities):
    urls = []
    if entities:
        for entity in entities:
            if entity.type == MessageEntity.URL:
                start = entity.offset
                end = entity.offset + entity.length
                raw = text.encode("utf-16-le")
                part = raw[start * 2:end * 2].decode("utf-16-le", errors="ignore")
                if part:
                    urls.append(part)
            elif entity.type == MessageEntity.TEXT_LINK:
                if entity.url:
                    urls.append(entity.url)
    if not urls:
        urls = re.findall(r"https?://[^\s<>\"]+", text or "")

    result = []
    for url in urls:
        if url not in result:
            result.append(url)
    return result

def build_signature(original_length: int):
    line1 = "➡️ Більше цікавої інформації у нашому чаті"
    line2 = "https://t.me/ua_in_ro"
    line3 = "🇺🇦 Украинцы в Румынии🇹🇩"
    line4 = "❤️ Список полезных каналов"

    signature = f"\n\n{line1}\n{line2}\n\n{line3}\n{line4}"
    entities = []
    offset = original_length

    entities.append(
        MessageEntity(
            type=MessageEntity.BOLD,
            offset=offset + utf16_len("\n\n"),
            length=utf16_len(line1),
        )
    )

    line3_offset = (
        offset
        + utf16_len("\n\n")
        + utf16_len(line1)
        + utf16_len("\n")
        + utf16_len(line2)
        + utf16_len("\n\n")
    )

    entities.append(
        MessageEntity(
            type=MessageEntity.BOLD,
            offset=line3_offset,
            length=utf16_len(line3),
        )
    )
    entities.append(
        MessageEntity(
            type=MessageEntity.UNDERLINE,
            offset=line3_offset,
            length=utf16_len(line3),
        )
    )
    entities.append(
        MessageEntity(
            type=MessageEntity.TEXT_LINK,
            offset=line3_offset,
            length=utf16_len(line3),
            url="https://t.me/ua_in_ro",
        )
    )

    line4_offset = line3_offset + utf16_len(line3) + utf16_len("\n")

    entities.append(
        MessageEntity(
            type=MessageEntity.BOLD,
            offset=line4_offset,
            length=utf16_len(line4),
        )
    )
    entities.append(
        MessageEntity(
            type=MessageEntity.TEXT_LINK,
            offset=line4_offset,
            length=utf16_len(line4),
            url="https://t.me/addlist/87244EkzpXxiZjFi",
        )
    )

    return signature, entities

def build_final_content(text, entities):
    text = text or ""
    entities = copy_entities(entities)
    original_length = utf16_len(text)
    signature, signature_entities = build_signature(original_length)
    return text + signature, entities + signature_entities

def get_occupied_times():
    conn = sqlite3.connect("posts.db")
    cursor = conn.cursor()
    cursor.execute("SELECT publish_time FROM scheduled_posts")
    rows = cursor.fetchall()
    conn.close()

    occupied = set()
    for row in rows:
        try:
            dt = datetime.fromisoformat(row[0])
            occupied.add(dt.strftime("%Y-%m-%d %H:%M"))
        except Exception:
            pass
    return occupied

def format_date_btn(dt: datetime) -> str:
    day_name = DAYS_UA[dt.weekday()]
    month_name = MONTHS_UA[dt.month]
    return f"{day_name}, {dt.day} {month_name}"

# =========================================================
# COMMANDS & RECEIVE POST
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Перешли мені новину.\n\nЯ покажу її перед публікацією.")

async def receive_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or message.chat.type != "private":
        return

    user_id = message.from_user.id
    state = user_states.get(user_id)

    if state and state.startswith("WAITING_TEXT:"):
        post_id = state.split(":", 1)[1]
        post = posts.get(post_id)
        if post:
            post["text"] = message.text or message.caption or ""
            post["entities"] = copy_entities(message.entities or message.caption_entities)
            urls = extract_urls(post["text"], post["entities"])
            if urls:
                post["preview_url"] = urls[0]
            
            user_states.pop(user_id, None)
            await message.reply_text("✅ Текст успішно оновлено!")
            await send_preview_by_chat_id(user_id, post_id, context)
            return

    if state and state.startswith("WAITING_MEDIA:"):
        post_id = state.split(":", 1)[1]
        post = posts.get(post_id)
        if post:
            if message.photo:
                post["photo_file_id"] = message.photo[-1].file_id
                post["video_file_id"] = None
            elif message.video:
                post["video_file_id"] = message.video.file_id
                post["photo_file_id"] = None
            else:
                await message.reply_text("❌ Будь ласка, надішліть фото або відео.")
                return

            user_states.pop(user_id, None)
            await message.reply_text("✅ Медіа успішно замінено!")
            await send_preview_by_chat_id(user_id, post_id, context)
            return

    if state and state.startswith("WAITING_TIME:"):
        parts = state.split(":")
        post_id = parts[1]
        date_str = parts[2]

        time_text = (message.text or "").strip()
        if re.match(r"^\d{1,2}:\d{2}$", time_text):
            h, m = time_text.split(":")
            time_text = f"{int(h):02d}:{int(m):02d}"
            user_states.pop(user_id, None)
            await process_schedule(update, context, post_id, date_str, time_text)
            return
        else:
            await message.reply_text("❌ Некоректний формат часу. Введіть час у форматі ГГ:ХХ (наприклад: 09:25 або 15:30):")
            return

    text = ""
    entities = []
    photo_file_id = None
    video_file_id = None
    preview_url = None

    if message.photo:
        photo_file_id = message.photo[-1].file_id
        text = message.caption or ""
        entities = copy_entities(message.caption_entities)
        urls = extract_urls(text, message.caption_entities)
        if urls:
            preview_url = urls[0]
    elif message.video:
        video_file_id = message.video.file_id
        text = message.caption or ""
        entities = copy_entities(message.caption_entities)
        urls = extract_urls(text, message.caption_entities)
        if urls:
            preview_url = urls[0]
    elif message.text:
        text = message.text
        entities = copy_entities(message.entities)
        urls = extract_urls(text, message.entities)
        if urls:
            preview_url = urls[0]
        if message.link_preview_options and message.link_preview_options.url:
            preview_url = message.link_preview_options.url
    else:
        await message.reply_text("❌ Цей тип повідомлення поки не підтримується.")
        return

    post_id = str(message.message_id)
    posts[post_id] = {
        "user_id": user_id,
        "text": text,
        "entities": entities,
        "photo_file_id": photo_file_id,
        "video_file_id": video_file_id,
        "channel_id": None,
        "preview_url": preview_url,
    }

    keyboard = []
    for channel_id, channel_name in CHANNELS.items():
        keyboard.append([InlineKeyboardButton(channel_name, callback_data=f"channel:{post_id}:{channel_id}")])
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel:{post_id}")])

    await message.reply_text("📢 Куди публікуємо?", reply_markup=InlineKeyboardMarkup(keyboard))

# =========================================================
# PREVIEW & EDITING
# =========================================================

async def send_preview_by_chat_id(chat_id, post_id, context):
    post = posts.get(post_id)
    if not post:
        return

    text = post["text"]
    entities = post.get("entities") or []
    final_text, final_entities = build_final_content(text, entities)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Редагувати текст", callback_data=f"edit_text:{post_id}"),
            InlineKeyboardButton("🖼 Замінити медіа", callback_data=f"edit_media:{post_id}")
        ],
        [InlineKeyboardButton("🚀 Опублікувати зараз", callback_data=f"publish:{post_id}")],
        [InlineKeyboardButton("⏰ Відкласти", callback_data=f"schedule:{post_id}")],
        [InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel:{post_id}")],
    ])

    preview_url = post.get("preview_url")

    if post["photo_file_id"]:
        await context.bot.send_photo(
            chat_id=chat_id, photo=post["photo_file_id"], caption=final_text,
            caption_entities=final_entities, reply_markup=keyboard
        )
    elif post["video_file_id"]:
        await context.bot.send_video(
            chat_id=chat_id, video=post["video_file_id"], caption=final_text,
            caption_entities=final_entities, reply_markup=keyboard
        )
    else:
        link_options = LinkPreviewOptions(
            is_disabled=False, url=preview_url, show_above_text=True, prefer_large_media=True
        )
        await context.bot.send_message(
            chat_id=chat_id, text=final_text, entities=final_entities,
            link_preview_options=link_options, reply_markup=keyboard
        )

async def send_preview(query, post_id, context):
    await send_preview_by_chat_id(query.from_user.id, post_id, context)

# =========================================================
# ACTIONS: CHANNEL, PUBLISH, SCHEDULE
# =========================================================

async def select_channel(query, context, post_id, channel_id):
    post = posts.get(post_id)
    if not post:
        await query.answer("Пост не знайдений.", show_alert=True)
        return

    post["channel_id"] = channel_id
    await query.answer()
    await query.edit_message_text("📢 Канал вибрано.\n\nЗараз покажу пост перед публікацією.")
    await send_preview(query, post_id, context)

async def publish_post(context, post):
    channel_id = post["channel_id"]
    text = post["text"]
    entities = post.get("entities") or []
    final_text, final_entities = build_final_content(text, entities)
    preview_url = post.get("preview_url")

    if post["photo_file_id"]:
        await context.bot.send_photo(
            chat_id=channel_id, photo=post["photo_file_id"],
            caption=final_text, caption_entities=final_entities
        )
    elif post["video_file_id"]:
        await context.bot.send_video(
            chat_id=channel_id, video=post["video_file_id"],
            caption=final_text, caption_entities=final_entities
        )
    else:
        link_options = LinkPreviewOptions(
            is_disabled=False, url=preview_url, show_above_text=True, prefer_large_media=True
        )
        await context.bot.send_message(
            chat_id=channel_id, text=final_text, entities=final_entities, link_preview_options=link_options
        )

async def publish_now(query, context, post_id):
    post = posts.get(post_id)
    if not post:
        await query.answer("Пост не знайдений.", show_alert=True)
        return

    try:
        await publish_post(context, post)
        await query.answer("Опубліковано ✅")
        await query.edit_message_text("✅ Пост опубліковано!")

        conn = sqlite3.connect("posts.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scheduled_posts WHERE post_id = ?", (post_id,))
        conn.commit()
        conn.close()

        posts.pop(post_id, None)
    except Exception:
        logger.exception("❌ Publish error")
        await query.answer("Помилка публікації.", show_alert=True)

async def schedule_post(query, context, post_id, target_date_str=None):
    now = datetime.now(KYIV)

    if target_date_str:
        try:
            current_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        except ValueError:
            current_date = now.date()
    else:
        current_date = now.date()

    prev_date = current_date - timedelta(days=1)
    next_date = current_date + timedelta(days=1)

    prev_str = prev_date.strftime("%Y-%m-%d")
    next_str = next_date.strftime("%Y-%m-%d")
    curr_str = current_date.strftime("%Y-%m-%d")

    times = [
        "08:25", "08:55", "09:25", "09:35", "09:56", "10:25", 
        "11:25", "12:25", "13:25", "14:25", "15:25", "16:25", 
        "17:25", "18:25", "19:25", "20:25", "21:25", "22:00"
    ]

    conn = sqlite3.connect("posts.db")
    cursor = conn.cursor()
    cursor.execute("SELECT publish_time, text FROM scheduled_posts")
    rows = cursor.fetchall()
    conn.close()

    occupied = set()
    day_posts = []

    for row in rows:
        try:
            dt = datetime.fromisoformat(row[0])
            slot_key = dt.strftime("%Y-%m-%d %H:%M")
            occupied.add(slot_key)

            if dt.date() == current_date:
                raw_text = (row[1] or "").strip().replace("\n", " ")
                short_text = raw_text[:40] + "..." if len(raw_text) > 40 else raw_text
                if not short_text:
                    short_text = "Медіаповідомлення"
                
                day_posts.append((dt.strftime("%H:%M"), short_text))
        except Exception:
            pass

    day_posts.sort(key=lambda x: x[0])
    keyboard = []

    keyboard.append([
        InlineKeyboardButton("←", callback_data=f"date:{post_id}:{prev_str}"),
        InlineKeyboardButton(f"🗓 {format_date_btn(current_date)}", callback_data="noop_date"),
        InlineKeyboardButton("→", callback_data=f"date:{post_id}:{next_str}")
    ])

    row = []
    for time_str in times:
        slot_dt_str = f"{curr_str} {time_str}"
        hour, minute = map(int, time_str.split(":"))
        slot_dt = datetime(current_date.year, current_date.month, current_date.day, hour, minute, tzinfo=KYIV)

        if slot_dt < now:
            continue

        if slot_dt_str in occupied:
            btn = InlineKeyboardButton(f"📌 {time_str}", callback_data="noop")
        else:
            btn = InlineKeyboardButton(time_str, callback_data=f"time:{post_id}:{curr_str}:{time_str}")

        row.append(btn)
        if len(row) == 3:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("✍️ Вибрати годину та хвилини", callback_data=f"manual_time:{post_id}:{curr_str}")])
    keyboard.append([InlineKeyboardButton("← Назад", callback_data=f"back:{post_id}")])

    text_content = f"🕒 **Час публікації**\n\n"
    if day_posts:
        text_content += f"**Заплановані пости на {format_date_btn(current_date)}:**\n"
        for time_p, title_p in day_posts:
            text_content += f"📍 **{time_p}** — *{title_p}*\n"
        text_content += "\n"
    else:
        text_content += f"На {format_date_btn(current_date)} немає запланованих постів.\n\n"

    text_content += f"Оберіть час з меню або надішліть його текстом:"

    await query.answer()
    await query.edit_message_text(
        text_content,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def process_schedule(update_or_query, context, post_id, date_string, time_string):
    post = posts.get(post_id)
    
    if hasattr(update_or_query, 'edit_message_text'):
        target_obj = update_or_query
    else:
        target_obj = update_or_query.effective_message

    if not post:
        if hasattr(update_or_query, 'answer'):
            await update_or_query.answer("Пост не знайдений.", show_alert=True)
        else:
            await target_obj.reply_text("Пост не знайдений.")
        return

    try:
        hour, minute = map(int, time_string.split(":"))
        year, month, day = map(int, date_string.split("-"))
        target = datetime(year, month, day, hour, minute, tzinfo=KYIV)
    except Exception:
        if hasattr(update_or_query, 'answer'):
            await update_or_query.answer("Помилка формату часу чи дати.", show_alert=True)
        else:
            await target_obj.reply_text("Помилка формату часу чи дати.")
        return

    now = datetime.now(KYIV)
    if target <= now:
        msg = "Цей час уже минув! Оберіть майбутній час."
        if hasattr(update_or_query, 'answer'):
            await update_or_query.answer(msg, show_alert=True)
        else:
            await target_obj.reply_text(f"❌ {msg}")
        return

    context.application.job_queue.run_once(
        scheduled_publish, when=target, data={"post_id": post_id}, name=f"post_{post_id}"
    )

    conn = sqlite3.connect("posts.db")
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO scheduled_posts 
        (post_id, user_id, channel_id, text, preview_url, photo_file_id, video_file_id, publish_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        post_id, post['user_id'], post['channel_id'], post['text'], 
        post['preview_url'], post['photo_file_id'], post['video_file_id'], target.isoformat()
    ))
    conn.commit()
    conn.close()

    success_msg = f"⏰ Пост заплановано на {target.strftime('%d.%m.%Y о %H:%M')}"
    if hasattr(update_or_query, 'answer'):
        await update_or_query.answer("Відкладено ✅")
        await update_or_query.edit_message_text(success_msg)
    else:
        await target_obj.reply_text(f"✅ {success_msg}")

async def scheduled_publish(context: ContextTypes.DEFAULT_TYPE):
    post_id = context.job.data["post_id"]
    post = posts.get(post_id)
    if not post:
        return

    try:
        await publish_post(context, post)
        posts.pop(post_id, None)

        conn = sqlite3.connect("posts.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scheduled_posts WHERE post_id = ?", (post_id,))
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("❌ BOT ERROR while scheduled publishing")

async def cancel_post(query, context, post_id):
    posts.pop(post_id, None)
    jobs = context.application.job_queue.get_jobs_by_name(f"post_{post_id}")
    for job in jobs:
        job.schedule_removal()

    await query.answer("Скасовано")
    await query.edit_message_text("❌ Пост скасовано.")

    conn = sqlite3.connect("posts.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scheduled_posts WHERE post_id = ?", (post_id,))
    conn.commit()
    conn.close()

# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "noop":
        await query.answer("Цей слот часу вже зайнятий!", show_alert=True)
        return
    elif data == "noop_date":
        await query.answer("Виберіть день стрілочками ← або →")
        return

    parts = data.split(":")
    action = parts[0]

    if action == "channel":
        await select_channel(query, context, parts[1], int(parts[2]))
    elif action == "publish":
        await publish_now(query, context, parts[1])
    elif action == "schedule":
        await schedule_post(query, context, parts[1])
    elif action == "date":
        await schedule_post(query, context, parts[1], parts[2])
    elif action == "time":
        await process_schedule(query, context, parts[1], parts[2], parts[3])
    elif action == "manual_time":
        post_id, date_str = parts[1], parts[2]
        user_states[query.from_user.id] = f"WAITING_TIME:{post_id}:{date_str}"
        await query.answer()
        await query.message.reply_text("✍️ Надішліть час для публікації текстом (наприклад: `09:25` або `18:40`):", parse_mode="Markdown")
    elif action == "edit_text":
        user_states[query.from_user.id] = f"WAITING_TEXT:{parts[1]}"
        await query.answer()
        await query.message.reply_text("✏️ Надішліть новий текст для цього поста:")
    elif action == "edit_media":
        user_states[query.from_user.id] = f"WAITING_MEDIA:{parts[1]}"
        await query.answer()
        await query.message.reply_text("🖼 Надішліть нове фото або відео для цього поста:")
    elif action == "back":
        await send_preview(query, parts[1], context)
    elif action == "cancel":
        await cancel_post(query, context, parts[1])

# =========================================================
# RESTORE & MAIN
# =========================================================

async def restore_scheduled_jobs(application):
    conn = sqlite3.connect("posts.db")
    cursor = conn.cursor()
    now = datetime.now(KYIV)
    cursor.execute("SELECT * FROM scheduled_posts")
    rows = cursor.fetchall()

    for row in rows:
        post_id, user_id, channel_id, text, preview_url, photo_id, video_id, target_str = row
        target = datetime.fromisoformat(target_str)

        posts[post_id] = {
            "user_id": user_id,
            "channel_id": channel_id,
            "text": text or "",
            "entities": [],
            "preview_url": preview_url,
            "photo_file_id": photo_id,
            "video_file_id": video_id
        }

        if target > now:
            application.job_queue.run_once(
                scheduled_publish, when=target, data={"post_id": post_id}, name=f"post_{post_id}"
            )

    conn.close()

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.ALL, receive_post))
    application.add_handler(CallbackQueryHandler(callbacks))

    async def _on_startup(context: ContextTypes.DEFAULT_TYPE):
        await restore_scheduled_jobs(context.application)

    application.job_queue.run_once(_on_startup, when=0)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
