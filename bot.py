import os
import re
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sqlite3

# =========================================================
# DATABASE INITIALIZATION
# =========================================================

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

# Запускаємо створення бази при старті скрипта
init_db()


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
# CONFIG
# =========================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

KYIV = ZoneInfo("Europe/Kyiv")

# Тестовий канал
CHANNELS = {
    -1004294187385: "🇷🇴 Українці в Румунії",
}

# Тимчасове зберігання постів
posts = {}


# =========================================================
# LOGGING
# =========================================================

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
    """
    Знаходимо URL саме з оригінальної новини.
    Потрібно, щоб Telegram використовував його для preview,
    а не URL з нашого підпису.
    """

    urls = []

    if entities:
        for entity in entities:

            # Звичайне посилання
            if entity.type == MessageEntity.URL:
                start = entity.offset
                end = entity.offset + entity.length

                # UTF-16 offsets Telegram -> Python
                raw = text.encode("utf-16-le")
                part = raw[start * 2:end * 2].decode(
                    "utf-16-le",
                    errors="ignore",
                )

                if part:
                    urls.append(part)

            # Приховане посилання
            elif entity.type == MessageEntity.TEXT_LINK:
                if entity.url:
                    urls.append(entity.url)

    # Fallback — якщо entities не дали URL
    if not urls:
        urls = re.findall(
            r"https?://[^\s<>\"]+",
            text or "",
        )

    # Прибираємо дублікати
    result = []

    for url in urls:
        if url not in result:
            result.append(url)

    return result


def build_signature(original_length: int):
    """
    Підпис:

    ➡️ Більше цікавої інформації у нашому чаті
    https://t.me/ua_in_ro

    🇺🇦 Украинцы в Румынии🇹🇩
    ❤️ Список полезных каналов
    """

    line1 = "➡️ Більше цікавої інформації у нашому чаті"
    line2 = "https://t.me/ua_in_ro"
    line3 = "🇺🇦 Украинцы в Румынии🇹🇩"
    line4 = "❤️ Список полезных каналов"

    signature = (
        "\n\n"
        + line1
        + "\n"
        + line2
        + "\n\n"
        + line3
        + "\n"
        + line4
    )

    entities = []

    # offset у UTF-16
    offset = original_length

    # line1 — bold
    entities.append(
        MessageEntity(
            type=MessageEntity.BOLD,
            offset=offset + utf16_len("\n\n"),
            length=utf16_len(line1),
        )
    )

    # line3 — bold + underline + URL
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

    # line4 — bold + URL
    line4_offset = (
        line3_offset
        + utf16_len(line3)
        + utf16_len("\n")
    )

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
    """
    Додаємо підпис, не ламаючи оригінальне форматування.
    """

    text = text or ""
    entities = copy_entities(entities)

    original_length = utf16_len(text)

    signature, signature_entities = build_signature(
        original_length
    )

    final_text = text + signature

    final_entities = entities + signature_entities

    return final_text, final_entities


# =========================================================
# COMMAND
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👋 Перешли мені новину.\n\n"
        "Я покажу її перед публікацією."
    )


# =========================================================
# RECEIVE POST
# =========================================================

async def receive_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message

    if not message:
        return

    # Працюємо тільки в особистому чаті з ботом
    if message.chat.type != "private":
        return

    text = ""
    entities = []
    photo_file_id = None
    video_file_id = None
    preview_url = None

    # -----------------------------------------------------
    # PHOTO
    # -----------------------------------------------------

    if message.photo:

        photo_file_id = message.photo[-1].file_id

        text = message.caption or ""
        entities = copy_entities(message.caption_entities)

        urls = extract_urls(
            text,
            message.caption_entities,
        )

        if urls:
            preview_url = urls[0]

    # -----------------------------------------------------
    # VIDEO
    # -----------------------------------------------------

    elif message.video:

        video_file_id = message.video.file_id

        text = message.caption or ""
        entities = copy_entities(message.caption_entities)

        urls = extract_urls(
            text,
            message.caption_entities,
        )

        if urls:
            preview_url = urls[0]

    # -----------------------------------------------------
    # TEXT
    # -----------------------------------------------------

    elif message.text:

        text = message.text
        entities = copy_entities(message.entities)

        urls = extract_urls(
            text,
            message.entities,
        )

        if urls:
            preview_url = urls[0]

        # Якщо Telegram вже згенерував картинку-прев'ю у вихідному повідомленні,
        # ми можемо перевірити наявність прикріпленого медіа з лінку:
        if message.link_preview_options and message.link_preview_options.url:
            preview_url = message.link_preview_options.url

    else:

        await message.reply_text(
            "❌ Цей тип повідомлення поки не підтримується."
        )

        return

    if not text and not photo_file_id and not video_file_id:

        await message.reply_text(
            "❌ Не знайшов текст або медіа."
        )

        return

    # -----------------------------------------------------
    # SAVE POST
    # -----------------------------------------------------

    post_id = str(message.message_id)

    posts[post_id] = {
        "user_id": message.from_user.id,
        "text": text,
        "entities": entities,
        "photo_file_id": photo_file_id,
        "video_file_id": video_file_id,
        "channel_id": None,
        "preview_url": preview_url,
    }

    logger.info(
        "📥 New post %s | preview=%s",
        post_id,
        preview_url,
    )

    # -----------------------------------------------------
    # CHANNEL MENU
    # -----------------------------------------------------

    keyboard = []

    for channel_id, channel_name in CHANNELS.items():

        keyboard.append(
            [
                InlineKeyboardButton(
                    channel_name,
                    callback_data=f"channel:{post_id}:{channel_id}",
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "❌ Скасувати",
                callback_data=f"cancel:{post_id}",
            )
        ]
    )

    await message.reply_text(
        "📢 Куди публікуємо?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# PREVIEW
# =========================================================

async def send_preview(
    query,
    post_id,
    context,
):

    post = posts.get(post_id)

    if not post:
        await query.answer(
            "Пост уже не знайдений.",
            show_alert=True,
        )
        return

    text = post["text"]
    entities = post["entities"]

    final_text, final_entities = build_final_content(
        text,
        entities,
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚀 Опублікувати зараз",
                    callback_data=f"publish:{post_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⏰ Відкласти",
                    callback_data=f"schedule:{post_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Скасувати",
                    callback_data=f"cancel:{post_id}",
                )
            ],
        ]
    )

    preview_url = post.get("preview_url")

    # -----------------------------------------------------
    # PHOTO
    # -----------------------------------------------------

    if post["photo_file_id"]:

        await context.bot.send_photo(
            chat_id=query.from_user.id,
            photo=post["photo_file_id"],
            caption=final_text,
            caption_entities=final_entities,
            reply_markup=keyboard,
        )

    # -----------------------------------------------------
    # VIDEO
    # -----------------------------------------------------

    elif post["video_file_id"]:

        await context.bot.send_video(
            chat_id=query.from_user.id,
            video=post["video_file_id"],
            caption=final_text,
            caption_entities=final_entities,
            reply_markup=keyboard,
        )

    # -----------------------------------------------------
    # TEXT + WEB PREVIEW
    # -----------------------------------------------------

    else:

        link_options = LinkPreviewOptions(
            is_disabled=False,
            url=preview_url,
            show_above_text=True,
            prefer_large_media=True,
        )

        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=final_text,
            entities=final_entities,
            link_preview_options=link_options,
            reply_markup=keyboard,
        )


# =========================================================
# SELECT CHANNEL
# =========================================================

async def select_channel(
    query,
    context,
    post_id,
    channel_id,
):

    post = posts.get(post_id)

    if not post:
        await query.answer(
            "Пост не знайдений.",
            show_alert=True,
        )
        return

    post["channel_id"] = channel_id

    await query.answer()

    await query.edit_message_text(
        "📢 Канал вибрано.\n\n"
        "Зараз покажу пост перед публікацією."
    )

    await send_preview(
        query,
        post_id,
        context,
    )


# =========================================================
# PUBLISH NOW
# =========================================================

async def publish_post(
    context,
    post,
):

    channel_id = post["channel_id"]

    text = post["text"]
    entities = post["entities"]

    final_text, final_entities = build_final_content(
        text,
        entities,
    )

    preview_url = post.get("preview_url")

    # -----------------------------------------------------
    # PHOTO
    # -----------------------------------------------------

    if post["photo_file_id"]:

        await context.bot.send_photo(
            chat_id=channel_id,
            photo=post["photo_file_id"],
            caption=final_text,
            caption_entities=final_entities,
        )

    # -----------------------------------------------------
    # VIDEO
    # -----------------------------------------------------

    elif post["video_file_id"]:

        await context.bot.send_video(
            chat_id=channel_id,
            video=post["video_file_id"],
            caption=final_text,
            caption_entities=final_entities,
        )

    # -----------------------------------------------------
    # TEXT + WEB PREVIEW
    # -----------------------------------------------------

    else:

        link_options = LinkPreviewOptions(
            is_disabled=False,
            url=preview_url,
            show_above_text=True,
            prefer_large_media=True,
        )

        await context.bot.send_message(
            chat_id=channel_id,
            text=final_text,
            entities=final_entities,
            link_preview_options=link_options,
        )


# =========================================================
# PUBLISH NOW BUTTON
# =========================================================

async def publish_now(
    query,
    context,
    post_id,
):

    post = posts.get(post_id)

    if not post:
        await query.answer(
            "Пост не знайдений.",
            show_alert=True,
        )
        return

    try:

        await publish_post(
            context,
            post,
        )

        await query.answer(
            "Опубліковано ✅"
        )

        await query.edit_message_text(
            "✅ Пост опубліковано!"
        )

        conn = sqlite3.connect("posts.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scheduled_posts WHERE post_id = ?", (post_id,))
        conn.commit()
        conn.close()


        posts.pop(post_id, None)

    except Exception as e:

        logger.exception(
            "❌ Publish error"
        )

        await query.answer(
            "Помилка публікації.",
            show_alert=True,
        )


# =========================================================
# SCHEDULE MENU
# =========================================================

async def schedule_post(
    query,
    context,
    post_id,
):

    times = [
        "08:25",
        "10:25",
        "11:25",
        "12:25",
        "13:25",
        "14:25",
        "15:25",
        "16:25",
        "17:25",
        "18:25",
        "19:25",
        "20:25",
        "21:25",
        "22:00",
    ]

    keyboard = []

    row = []

    for time in times:

        row.append(
            InlineKeyboardButton(
                time,
                callback_data=f"time:{post_id}:{time}",
            )
        )

        if len(row) == 3:

            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"back:{post_id}",
            )
        ]
    )

    await query.answer()

    await query.edit_message_text(
        "⏰ Обери час публікації:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# CHOOSE TIME
# =========================================================

async def choose_time(query, context, post_id, time_string):
    post = posts.get(post_id)

    if not post:
        await query.answer("Пост не знайдений.", show_alert=True)
        return

    try:
        hour, minute = map(int, time_string.split(":"))
    except ValueError:
        logger.error("❌ Invalid time format received: %s", time_string)
        await query.answer("Помилка формату часу.", show_alert=True)
        return

    now = datetime.now(KYIV)

    target = now.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )

    if target <= now:
        target += timedelta(days=1)

    logger.info("⏰ Scheduling post %s for %s", post_id, target)

    context.application.job_queue.run_once(
        scheduled_publish,
        when=target,
        data={"post_id": post_id},
        name=f"post_{post_id}",
    )

    conn = sqlite3.connect("posts.db")
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO scheduled_posts 
        (post_id, user_id, channel_id, text, preview_url, photo_file_id, video_file_id, publish_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        post_id, 
        post['user_id'], 
        post['channel_id'], 
        post['text'], 
        post['preview_url'], 
        post['photo_file_id'], 
        post['video_file_id'], 
        target.isoformat()
    ))
    conn.commit()
    conn.close()

    await query.answer("Відкладено ✅")
    await query.edit_message_text(
        f"⏰ Пост заплановано на {target.strftime('%d.%m %H:%M')}"
    )



# =========================================================
# SCHEDULED PUBLISH
# =========================================================

async def scheduled_publish(
    context: ContextTypes.DEFAULT_TYPE,
):

    post_id = context.job.data["post_id"]

    logger.info(
        "⏰ Scheduled job STARTED: %s",
        post_id,
    )

    post = posts.get(post_id)

    if not post:

        logger.error(
            "❌ Scheduled post %s not found",
            post_id,
        )

        return

    try:

        logger.info(
            "📤 Publishing scheduled post %s",
            post_id,
        )

        await publish_post(
            context,
            post,
        )

        logger.info(
            "✅ Scheduled post published: %s",
            post_id,
        )

        posts.pop(
            post_id,
            None,
        )
        
        conn = sqlite3.connect("posts.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scheduled_posts WHERE post_id = ?", (post_id,))
        conn.commit()
        conn.close()


    except Exception:

        logger.exception(
            "❌ BOT ERROR while scheduled publishing"
        )


# =========================================================
# CANCEL
# =========================================================

async def cancel_post(
    query,
    context,
    post_id,
):

    posts.pop(
        post_id,
        None,
    )

    # Видаляємо заплановану задачу
    jobs = context.application.job_queue.get_jobs_by_name(
        f"post_{post_id}"
    )

    for job in jobs:
        job.schedule_removal()

    await query.answer(
        "Скасовано"
    )

    await query.edit_message_text(
        "❌ Пост скасовано."
    )
    
    conn = sqlite3.connect("posts.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scheduled_posts WHERE post_id = ?", (post_id,))
    conn.commit()
    conn.close()



# =========================================================
# CALLBACKS
# =========================================================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    data = query.data

    parts = data.split(":",2)

    action = parts[0]

    # -----------------------------------------------------
    # CHANNEL
    # -----------------------------------------------------

    if action == "channel":

        post_id = parts[1]
        channel_id = int(parts[2])

        await select_channel(
            query,
            context,
            post_id,
            channel_id,
        )

    # -----------------------------------------------------
    # PUBLISH
    # -----------------------------------------------------

    elif action == "publish":

        post_id = parts[1]

        await publish_now(
            query,
            context,
            post_id,
        )

    # -----------------------------------------------------
    # SCHEDULE
    # -----------------------------------------------------

    elif action == "schedule":

        post_id = parts[1]

        await schedule_post(
            query,
            context,
            post_id,
        )

    # -----------------------------------------------------
    # TIME
    # -----------------------------------------------------

    elif action == "time":

        post_id = parts[1]
        time_string = parts[2]

        await choose_time(
            query,
            context,
            post_id,
            time_string,
        )

    # -----------------------------------------------------
    # BACK
    # -----------------------------------------------------

    elif action == "back":

        post_id = parts[1]

        await send_preview(
            query,
            post_id,
            context,
        )

    # -----------------------------------------------------
    # CANCEL
    # -----------------------------------------------------

    elif action == "cancel":

        post_id = parts[1]

        await cancel_post(
            query,
            context,
            post_id,
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context,
):

    logger.exception(
        "BOT ERROR:",
        exc_info=context.error,
    )



# Відновлення пропущених постів

async def restore_scheduled_jobs(application):
    conn = sqlite3.connect("posts.db")
    cursor = conn.cursor()
    now = datetime.now(KYIV)
    
    cursor.execute("SELECT * FROM scheduled_posts")
    rows = cursor.fetchall()
    
    for row in rows:
        post_id, user_id, channel_id, text, preview_url, photo_id, video_id, target_str = row
        target = datetime.fromisoformat(target_str)
        
        # Відновлюємо пост у пам'яті бота
        posts[post_id] = {
            "user_id": user_id,
            "channel_id": channel_id,
            "text": text,
            "entities": [],
            "preview_url": preview_url,
            "photo_file_id": photo_id,
            "video_file_id": video_id
        }
        
        if target <= now:
            # ⚠️ ЧАС МИНУВ, поки бот був офлайн: надсилаємо вам сповіщення і сам пост!
            channel_name = CHANNELS.get(channel_id, "Канал")
            
            await application.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ **ПРОПУЩЕНО ПУБЛІКАЦІЮ!**\n\n"
                    f"📌 **Канал:** {channel_name}\n"
                    f"⏰ **Час:** {target.strftime('%d.%m о %H:%M')}\n\n"
                    f"Оберіть під постом нижче, що з ним зробити:"
                ),
                parse_mode="Markdown"
            )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Опублікувати зараз", callback_data=f"publish:{post_id}")],
                [InlineKeyboardButton("⏰ Перепланувати", callback_data=f"schedule:{post_id}")],
                [InlineKeyboardButton("❌ Видалити", callback_data=f"cancel:{post_id}")]
            ])
            
            final_text, final_entities = build_final_content(text, [])
            
            if photo_id:
                await application.bot.send_photo(
                    chat_id=user_id, photo=photo_id, caption=final_text, 
                    caption_entities=final_entities, reply_markup=keyboard
                )
            elif video_id:
                await application.bot.send_video(
                    chat_id=user_id, video=video_id, caption=final_text, 
                    caption_entities=final_entities, reply_markup=keyboard
                )
            else:
                link_options = LinkPreviewOptions(
                    is_disabled=False, url=preview_url, 
                    show_above_text=True, prefer_large_media=True
                )
                await application.bot.send_message(
                    chat_id=user_id, text=final_text, entities=final_entities, 
                    link_preview_options=link_options, reply_markup=keyboard
                )
        else:
            # ✅ ЧАС МЕЖІ НЕ ПЕРЕЙШОВ: відновлюємо таймер у scheduler
            application.job_queue.run_once(
                scheduled_publish,
                when=target,
                data={"post_id": post_id},
                name=f"post_{post_id}"
            )
            
    conn.close()




# =========================================================
# MAIN
# =========================================================

def main():

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ALL,
            receive_post,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callbacks,
        )
    )

    application.add_error_handler(
        error_handler
    )
 

    logger.info("🤖 Bot starting...")

    # Безпечний запуск відновлення завдань при старті
    async def _on_startup(context: ContextTypes.DEFAULT_TYPE):
        await restore_scheduled_jobs(context.application)

    application.job_queue.run_once(_on_startup, when=0)

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )



if __name__ == "__main__":
    main()
