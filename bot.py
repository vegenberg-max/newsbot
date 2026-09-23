import os
import uuid
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from telegram.constants import MessageEntityType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# НАЛАШТУВАННЯ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

KYIV = ZoneInfo("Europe/Kyiv")


# ============================================================
# КАНАЛИ
# ============================================================

CHANNELS = {
    "-1004294187385": {
        "name": "🇷🇴 Тестовий канал",

        # Тут:
        # 1) перший рядок жирний
        # 2) URL
        # 3) ОДНА порожня строка
        # 4) Украинцы — жирний + підкреслений + посилання
        # 5) Список — жирний + посилання
        "signature_text": (
            "➡️ Більше цікавої інформації у нашому чаті\n"
            "https://t.me/ua_in_ro\n\n"
            "🇺🇦 Украинцы в Румынии🇹🇩\n"
            "❤️ Список полезных каналов"
        ),

        "signature_entities": [
            # Перший рядок — жирний
            {
                "type": MessageEntityType.BOLD,
                "offset": 0,
                "length": 41,
            },

            # Украинцы — жирний + підкреслений + URL
            {
                "type": MessageEntityType.BOLD,
                "offset": 73,
                "length": 30,
            },
            {
                "type": MessageEntityType.UNDERLINE,
                "offset": 73,
                "length": 30,
            },
            {
                "type": MessageEntityType.TEXT_LINK,
                "offset": 73,
                "length": 30,
                "url": "https://t.me/ua_in_ro",
            },

            # Список — жирний + URL
            {
                "type": MessageEntityType.BOLD,
                "offset": 104,
                "length": 27,
            },
            {
                "type": MessageEntityType.TEXT_LINK,
                "offset": 104,
                "length": 27,
                "url": "https://t.me/addlist/87244EkzpXxiZjFi",
            },
        ],
    }
}


# ============================================================
# ПОСТИ
# ============================================================

posts = {}


# ============================================================
# HTTP SERVER ДЛЯ RENDER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        pass


def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()


# ============================================================
# ENTITY HELPERS
# ============================================================

def copy_entities(entities):
    """
    Перетворює Telegram MessageEntity у словники,
    щоб їх можна було безпечно зберігати в пам'яті.
    """

    result = []

    for entity in entities or []:

        item = {
            "type": entity.type,
            "offset": entity.offset,
            "length": entity.length,
        }

        if entity.url:
            item["url"] = entity.url

        if entity.user:
            item["user"] = entity.user

        if entity.language:
            item["language"] = entity.language

        if entity.custom_emoji_id:
            item["custom_emoji_id"] = entity.custom_emoji_id

        result.append(item)

    return result


def make_entity(item, offset_shift=0):
    """
    Створює MessageEntity для Telegram.
    """

    kwargs = {
        "type": item["type"],
        "offset": item["offset"] + offset_shift,
        "length": item["length"],
    }

    if item.get("url"):
        kwargs["url"] = item["url"]

    if item.get("user"):
        kwargs["user"] = item["user"]

    if item.get("language"):
        kwargs["language"] = item["language"]

    if item.get("custom_emoji_id"):
        kwargs["custom_emoji_id"] = item["custom_emoji_id"]

    return MessageEntity(**kwargs)


def utf16_length(text):
    """
    Telegram offsets працюють у UTF-16.
    """

    return len(text.encode("utf-16-le")) // 2


def build_final_content(post):
    """
    Об'єднує оригінальний текст/форматування
    з автоматичним підписом.
    """

    original_text = post["text"] or ""
    original_entities = post["entities"] or []

    channel = CHANNELS[post["channel_id"]]

    signature_text = channel["signature_text"]

    if original_text:
        final_text = (
            original_text
            + "\n\n"
            + signature_text
        )

        signature_offset = utf16_length(
            original_text + "\n\n"
        )

    else:
        final_text = signature_text
        signature_offset = 0

    final_entities = []

    # Оригінальне форматування
    for entity in original_entities:
        final_entities.append(
            make_entity(entity)
        )

    # Форматування підпису
    for entity in channel["signature_entities"]:
        final_entities.append(
            make_entity(
                entity,
                signature_offset
            )
        )

    return final_text, final_entities


# ============================================================
# КНОПКИ
# ============================================================

def action_buttons(post_id):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚀 Опублікувати зараз",
                callback_data=f"publish:{post_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "⏰ Відкласти",
                callback_data=f"schedule:{post_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Скасувати",
                callback_data=f"cancel:{post_id}"
            )
        ]
    ])


def time_buttons(post_id):

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

    rows = []
    row = []

    for time in times:

        row.append(
            InlineKeyboardButton(
                time,
                callback_data=f"time|{post_id}|{time}"
            )
        )

        if len(row) == 3:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([
        InlineKeyboardButton(
            "⬅️ Назад",
            callback_data=f"back:{post_id}"
        )
    ])

    return InlineKeyboardMarkup(rows)


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👋 <b>Привіт!</b>\n\n"
        "Перешли мені готовий пост.\n\n"
        "Я покажу його готовий вигляд "
        "перед публікацією.",
        parse_mode="HTML"
    )


# ============================================================
# ОТРИМАННЯ ПОСТА
# ============================================================

async def receive_post(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.message

    if not message:
        return

    post_id = uuid.uuid4().hex[:10]

    text = ""
    entities = []

    photo_file_id = None
    video_file_id = None

    # --------------------------------------------------------
    # ФОТО
    # --------------------------------------------------------

    if message.photo:

        photo_file_id = message.photo[-1].file_id

        text = message.caption or ""

        entities = copy_entities(
            message.caption_entities
        )

    # --------------------------------------------------------
    # ВІДЕО
    # --------------------------------------------------------

    elif message.video:

        video_file_id = message.video.file_id

        text = message.caption or ""

        entities = copy_entities(
            message.caption_entities
        )

    # --------------------------------------------------------
    # ТЕКСТ
    # --------------------------------------------------------

    elif message.text:

        text = message.text

        entities = copy_entities(
            message.entities
        )

    else:

        await message.reply_text(
            "⚠️ Цей тип повідомлення поки не підтримується."
        )

        return

    posts[post_id] = {
        "user_id": update.effective_user.id,
        "text": text,
        "entities": entities,
        "photo_file_id": photo_file_id,
        "video_file_id": video_file_id,
        "channel_id": None,
    }

    # --------------------------------------------------------
    # ВИБІР КАНАЛУ
    # --------------------------------------------------------

    buttons = []

    for channel_id, channel in CHANNELS.items():

        buttons.append([
            InlineKeyboardButton(
                channel["name"],
                callback_data=f"channel:{post_id}:{channel_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "❌ Скасувати",
            callback_data=f"cancel:{post_id}"
        )
    ])

    await message.reply_text(
        "📢 <b>Куди публікуємо?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# PREVIEW
# ============================================================

async def send_preview(
    chat_id,
    post_id,
    context
):

    post = posts[post_id]

    final_text, final_entities = (
        build_final_content(post)
    )

    # --------------------------------------------------------
    # ФОТО
    # --------------------------------------------------------

    if post["photo_file_id"]:

        await context.bot.send_photo(
            chat_id=chat_id,
            photo=post["photo_file_id"],
            caption=final_text,
            caption_entities=final_entities,
            reply_markup=action_buttons(post_id)
        )

        return

    # --------------------------------------------------------
    # ВІДЕО
    # --------------------------------------------------------

    if post["video_file_id"]:

        await context.bot.send_video(
            chat_id=chat_id,
            video=post["video_file_id"],
            caption=final_text,
            caption_entities=final_entities,
            reply_markup=action_buttons(post_id)
        )

        return

    # --------------------------------------------------------
    # ТЕКСТ
    # --------------------------------------------------------

    await context.bot.send_message(
        chat_id=chat_id,
        text=final_text,
        entities=final_entities,
        disable_web_page_preview=False,
        reply_markup=action_buttons(post_id)
    )


# ============================================================
# ВИБІР КАНАЛУ
# ============================================================

async def select_channel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    _, post_id, channel_id = (
        query.data.split(":", 2)
    )

    if post_id not in posts:

        await query.edit_message_text(
            "⚠️ Пост більше недоступний."
        )

        return

    posts[post_id]["channel_id"] = channel_id

    try:
        await query.message.delete()
    except Exception:
        pass

    await send_preview(
        query.from_user.id,
        post_id,
        context
    )


# ============================================================
# ПУБЛІКАЦІЯ
# ============================================================

async def publish_post(
    post_id,
    context
):

    if post_id not in posts:
        return

    post = posts[post_id]

    channel_id = post["channel_id"]

    final_text, final_entities = (
        build_final_content(post)
    )

    # --------------------------------------------------------
    # ФОТО
    # --------------------------------------------------------

    if post["photo_file_id"]:

        await context.bot.send_photo(
            chat_id=channel_id,
            photo=post["photo_file_id"],
            caption=final_text,
            caption_entities=final_entities
        )

    # --------------------------------------------------------
    # ВІДЕО
    # --------------------------------------------------------

    elif post["video_file_id"]:

        await context.bot.send_video(
            chat_id=channel_id,
            video=post["video_file_id"],
            caption=final_text,
            caption_entities=final_entities
        )

    # --------------------------------------------------------
    # ТЕКСТ
    # --------------------------------------------------------

    else:

        await context.bot.send_message(
            chat_id=channel_id,
            text=final_text,
            entities=final_entities,
            disable_web_page_preview=False
        )


# ============================================================
# ОПУБЛІКУВАТИ ЗАРАЗ
# ============================================================

async def publish_now(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    _, post_id = (
        query.data.split(":", 1)
    )

    if post_id not in posts:

        await query.edit_message_text(
            "⚠️ Пост більше недоступний."
        )

        return

    try:

        await publish_post(
            post_id,
            context
        )

        await query.message.reply_text(
            "✅ Опубліковано!"
        )

        del posts[post_id]

    except Exception as e:

        print(
            f"Publish error: {e}"
        )

        await query.message.reply_text(
            "❌ Не вдалося опублікувати пост."
        )


# ============================================================
# ВІДКЛАСТИ
# ============================================================

async def schedule_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    _, post_id = (
        query.data.split(":", 1)
    )

    if post_id not in posts:
        return

    await query.edit_message_reply_markup(
        reply_markup=time_buttons(post_id)
    )


# ============================================================
# НАЗАД
# ============================================================

async def back_to_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    _, post_id = (
        query.data.split(":", 1)
    )

    if post_id not in posts:
        return

    await query.edit_message_reply_markup(
        reply_markup=action_buttons(post_id)
    )


# ============================================================
# ВИБІР ЧАСУ
# ============================================================

async def choose_time(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    _, post_id, time_string = (
        query.data.split("|", 2)
    )

    if post_id not in posts:
        return

    hour, minute = map(
        int,
        time_string.split(":")
    )

    now = datetime.now(KYIV)

    target = now.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0
    )

    if target <= now:
        target += timedelta(days=1)

    delay = (
        target - now
    ).total_seconds()

    context.application.job_queue.run_once(
        scheduled_publish,
        when=delay,
        data=post_id,
        name=post_id
    )

    await query.edit_message_reply_markup(
        reply_markup=None
    )

    await query.message.reply_text(
        "⏰ Заплановано!\n\n"
        f"📅 {target.strftime('%d.%m.%Y')}\n"
        f"🕐 {target.strftime('%H:%M')}\n\n"
        "Час за Києвом."
    )


# ============================================================
# ЗАПЛАНОВАНА ПУБЛІКАЦІЯ
# ============================================================

async def scheduled_publish(
    context: ContextTypes.DEFAULT_TYPE
):

    post_id = context.job.data

    if post_id not in posts:
        return

    try:

        await publish_post(
            post_id,
            context
        )

        del posts[post_id]

    except Exception as e:

        print(
            f"Scheduled publish error: {e}"
        )


# ============================================================
# СКАСУВАННЯ
# ============================================================

async def cancel_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    _, post_id = (
        query.data.split(":", 1)
    )

    jobs = (
        context.application
        .job_queue
        .get_jobs_by_name(post_id)
    )

    for job in jobs:
        job.schedule_removal()

    posts.pop(post_id, None)

    try:
        await query.message.delete()
    except Exception:
        await query.edit_message_text(
            "❌ Скасовано."
        )


# ============================================================
# ПОМИЛКИ
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "BOT ERROR:",
        context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не знайдено."
        )

    # HTTP для Render Web Service
    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # HANDLERS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT |
            filters.PHOTO |
            filters.VIDEO,
            receive_post
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            select_channel,
            pattern=r"^channel:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            publish_now,
            pattern=r"^publish:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            schedule_post,
            pattern=r"^schedule:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            choose_time,
            pattern=r"^time\|"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            back_to_preview,
            pattern=r"^back:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_post,
            pattern=r"^cancel:"
        )
    )

    application.add_error_handler(
        error_handler
    )

    print("🤖 Bot started")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
