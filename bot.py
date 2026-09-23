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
)
from telegram.constants import ParseMode
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

        "signature": (
            '<b>➡️ Більше цікавої інформації у нашому чаті</b>\n'
            'https://t.me/ua_in_ro\n'
            '<b><u><a href="https://t.me/ua_in_ro">'
            '🇺🇦 Украинцы в Румынии🇹🇩'
            '</a></u></b>\n'
            '<b><a href="https://t.me/addlist/87244EkzpXxiZjFi">'
            '❤️ Список полезных каналов'
            '</a></b>'
        ),
    }
}

# ============================================================
# ТИМЧАСОВЕ ЗБЕРІГАННЯ ПОСТІВ
# ============================================================

posts = {}


# ============================================================
# HTTP SERVER ДЛЯ RENDER WEB SERVICE
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        return


def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()


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

    for t in times:

        row.append(
            InlineKeyboardButton(
                t,
                callback_data=f"time|{post_id}|{t}"
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
        "Я покажу його попередній перегляд, "
        "додам підпис і дам вибрати час публікації.",
        parse_mode=ParseMode.HTML
    )


# ============================================================
# ОТРИМАННЯ ПОВІДОМЛЕННЯ
# ============================================================

async def receive_post(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.message

    if not message:
        return

    post_id = uuid.uuid4().hex[:10]

    photo_file_id = None
    video_file_id = None
    text = ""
    entities = []

    # -------------------------------
    # ФОТО
    # -------------------------------

    if message.photo:

        photo_file_id = message.photo[-1].file_id

        text = message.caption or ""

        entities = message.caption_entities or []

    # -------------------------------
    # ВІДЕО
    # -------------------------------

    elif message.video:

        video_file_id = message.video.file_id

        text = message.caption or ""

        entities = message.caption_entities or []

    # -------------------------------
    # ТЕКСТ
    # -------------------------------

    elif message.text:

        text = message.text

        entities = message.entities or []

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

    # ========================================================
    # ВИБІР КАНАЛУ
    # ========================================================

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
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# ФОРМУВАННЯ ТЕКСТУ
# ============================================================

def build_text(post):

    original = post["text"]

    channel_id = post["channel_id"]

    signature = CHANNELS[channel_id]["signature"]

    if original:

        return original + "\n\n" + signature

    return signature


# ============================================================
# PREVIEW
# ============================================================

async def send_preview(chat_id, post_id, context):

    post = posts[post_id]

    final_text = build_text(post)

    # ========================================================
    # ФОТО
    # ========================================================

    if post["photo_file_id"]:

        await context.bot.send_photo(
            chat_id=chat_id,

            photo=post["photo_file_id"],

            caption=final_text,

            parse_mode=ParseMode.HTML,

            reply_markup=action_buttons(post_id)
        )

        return

    # ========================================================
    # ВІДЕО
    # ========================================================

    if post["video_file_id"]:

        await context.bot.send_video(
            chat_id=chat_id,

            video=post["video_file_id"],

            caption=final_text,

            parse_mode=ParseMode.HTML,

            reply_markup=action_buttons(post_id)
        )

        return

    # ========================================================
    # ТЕКСТ
    # ========================================================

    await context.bot.send_message(
        chat_id=chat_id,

        text=final_text,

        parse_mode=ParseMode.HTML,

        disable_web_page_preview=False,

        reply_markup=action_buttons(post_id)
    )


# ============================================================
# ВИБІР КАНАЛУ
# ============================================================

async def select_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    _, post_id, channel_id = query.data.split(":", 2)

    if post_id not in posts:

        await query.edit_message_text(
            "⚠️ Пост більше недоступний."
        )

        return

    posts[post_id]["channel_id"] = channel_id

    # Видаляємо повідомлення
    # "Куди публікуємо?"

    try:
        await query.message.delete()
    except Exception:
        pass

    # Надсилаємо справжній preview
    await send_preview(
        query.from_user.id,
        post_id,
        context
    )


# ============================================================
# ПУБЛІКАЦІЯ
# ============================================================

async def publish_post(post_id, context):

    if post_id not in posts:
        return

    post = posts[post_id]

    channel_id = post["channel_id"]

    final_text = build_text(post)

    # ========================================================
    # ФОТО
    # ========================================================

    if post["photo_file_id"]:

        await context.bot.send_photo(
            chat_id=channel_id,

            photo=post["photo_file_id"],

            caption=final_text,

            parse_mode=ParseMode.HTML
        )

    # ========================================================
    # ВІДЕО
    # ========================================================

    elif post["video_file_id"]:

        await context.bot.send_video(
            chat_id=channel_id,

            video=post["video_file_id"],

            caption=final_text,

            parse_mode=ParseMode.HTML
        )

    # ========================================================
    # ТЕКСТ
    # ========================================================

    else:

        await context.bot.send_message(
            chat_id=channel_id,

            text=final_text,

            parse_mode=ParseMode.HTML,

            disable_web_page_preview=False
        )


# ============================================================
# ОПУБЛІКУВАТИ ЗАРАЗ
# ============================================================

async def publish_now(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    _, post_id = query.data.split(":", 1)

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
            "✅ <b>Опубліковано!</b>",
            parse_mode=ParseMode.HTML
        )

        del posts[post_id]

    except Exception as e:

        await query.message.reply_text(
            f"❌ Помилка публікації:\n{e}"
        )


# ============================================================
# ВІДКЛАСТИ
# ============================================================

async def schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    _, post_id = query.data.split(":", 1)

    if post_id not in posts:
        return

    await query.edit_message_reply_markup(
        reply_markup=time_buttons(post_id)
    )


# ============================================================
# НАЗАД
# ============================================================

async def back_to_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    _, post_id = query.data.split(":", 1)

    if post_id not in posts:
        return

    await query.edit_message_reply_markup(
        reply_markup=action_buttons(post_id)
    )


# ============================================================
# ВИБІР ЧАСУ
# ============================================================

async def choose_time(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    _, post_id, time_string = query.data.split("|", 2)

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

    # Якщо цей час вже пройшов —
    # переносимо на завтра.

    if target <= now:

        target += timedelta(days=1)

    delay = (
        target - now
    ).total_seconds()

    job = context.application.job_queue.run_once(
        scheduled_publish,

        when=delay,

        data=post_id,

        name=post_id
    )

    posts[post_id]["job"] = job

    await query.edit_message_reply_markup(
        reply_markup=None
    )

    await query.message.reply_text(
        "⏰ <b>Заплановано!</b>\n\n"
        f"📅 {target.strftime('%d.%m.%Y')}\n"
        f"🕐 {target.strftime('%H:%M')}\n\n"
        "Час за Києвом.",
        parse_mode=ParseMode.HTML
    )


# ============================================================
# ЗАПЛАНОВАНА ПУБЛІКАЦІЯ
# ============================================================

async def scheduled_publish(context: ContextTypes.DEFAULT_TYPE):

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
# СКАСУВАТИ
# ============================================================

async def cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    _, post_id = query.data.split(":", 1)

    # Видаляємо запланований job

    jobs = (
        context.application
        .job_queue
        .get_jobs_by_name(post_id)
    )

    for job in jobs:
        job.schedule_removal()

    if post_id in posts:
        del posts[post_id]

    try:

        await query.message.delete()

    except Exception:

        await query.edit_message_text(
            "❌ Скасовано."
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN не знайдено."
        )

    # Web server для Render

    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    # Telegram bot

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

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

    print("🤖 Bot started")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
