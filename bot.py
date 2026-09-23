import os
import html
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

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

BOT_TOKEN = os.environ.get("BOT_TOKEN")

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Тестовий канал
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

# Тут тимчасово зберігаються пости.
# Пізніше зробимо SQLite/PostgreSQL, щоб вони не зникали
# після перезапуску Render.
posts = {}


# ============================================================
# КНОПКИ
# ============================================================

def main_buttons(post_id: str):
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


def time_buttons(post_id: str):
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

    buttons = []

    row = []

    for time in times:
        row.append(
            InlineKeyboardButton(
                time,
                callback_data=f"time|{post_id}|{time}"
            )
        )

        if len(row) == 3:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Назад",
            callback_data=f"back:{post_id}"
        )
    ])

    return InlineKeyboardMarkup(buttons)


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Привіт!</b>\n\n"
        "Перешли мені готовий пост — текст, фото або фото з текстом.\n\n"
        "Я покажу тобі, як він виглядатиме після додавання підпису."
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML
    )


# ============================================================
# ОТРИМАННЯ ПОСТА
# ============================================================

async def receive_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message:
        return

    # --------------------------------------------------------
    # Визначаємо тип повідомлення
    # --------------------------------------------------------

    post_id = str(uuid.uuid4())[:8]

    photo_file_id = None
    video_file_id = None

    text = None
    entities = None

    # Фото
    if message.photo:
        photo_file_id = message.photo[-1].file_id
        text = message.caption or ""
        entities = message.caption_entities or []

    # Відео
    elif message.video:
        video_file_id = message.video.file_id
        text = message.caption or ""
        entities = message.caption_entities or []

    # Текст
    elif message.text:
        text = message.text
        entities = message.entities or []

    else:
        await message.reply_text(
            "⚠️ Цей тип повідомлення поки не підтримується."
        )
        return

    # --------------------------------------------------------
    # Зберігаємо пост
    # --------------------------------------------------------

    posts[post_id] = {
        "user_id": update.effective_user.id,
        "text": text or "",
        "entities": entities or [],
        "photo_file_id": photo_file_id,
        "video_file_id": video_file_id,
        "channel_id": None,
        "scheduled_job": None,
    }

    # --------------------------------------------------------
    # Вибір каналу
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
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# ВИБІР КАНАЛУ
# ============================================================

async def select_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split(":", 2)

    if len(data) != 3:
        return

    _, post_id, channel_id = data

    if post_id not in posts:
        await query.edit_message_text(
            "⚠️ Цей пост більше недоступний."
        )
        return

    posts[post_id]["channel_id"] = channel_id

    await show_preview(query, post_id)


# ============================================================
# ФОРМУЄМО ПІДПИС
# ============================================================

def get_final_text(post):
    channel_id = post["channel_id"]

    signature = CHANNELS[channel_id]["signature"]

    original_text = post["text"] or ""

    if original_text:
        return f"{original_text}\n\n{signature}"

    return signature


# ============================================================
# PREVIEW
# ============================================================

async def show_preview(query, post_id):
    post = posts[post_id]

    final_text = get_final_text(post)

    # Видаляємо старе повідомлення з кнопками,
    # щоб preview виглядав як справжній пост.
    try:
        await query.message.delete()
    except Exception:
        pass

    # --------------------------------------------------------
    # Фото
    # --------------------------------------------------------

    if post["photo_file_id"]:
        await query.message.chat.send_photo(
            photo=post["photo_file_id"],
            caption=final_text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_buttons(post_id)
        )

        return

    # --------------------------------------------------------
    # Відео
    # --------------------------------------------------------

    if post["video_file_id"]:
        await query.message.chat.send_video(
            video=post["video_file_id"],
            caption=final_text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_buttons(post_id)
        )

        return

    # --------------------------------------------------------
    # Звичайний текст
    # --------------------------------------------------------

    await query.message.chat.send_message(
        text=final_text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_buttons(post_id),
        disable_web_page_preview=False
    )


# ============================================================
# ПУБЛІКАЦІЯ
# ============================================================

async def publish_post(post_id, context):
    if post_id not in posts:
        return

    post = posts[post_id]

    channel_id = post["channel_id"]

    if not channel_id:
        return

    final_text = get_final_text(post)

    # Фото
    if post["photo_file_id"]:
        await context.bot.send_photo(
            chat_id=channel_id,
            photo=post["photo_file_id"],
            caption=final_text,
            parse_mode=ParseMode.HTML
        )

    # Відео
    elif post["video_file_id"]:
        await context.bot.send_video(
            chat_id=channel_id,
            video=post["video_file_id"],
            caption=final_text,
            parse_mode=ParseMode.HTML
        )

    # Текст
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
        await publish_post(post_id, context)

        # Видаляємо кнопки після публікації
        try:
            await query.message.edit_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        await query.message.reply_text(
            "✅ <b>Опубліковано!</b>",
            parse_mode=ParseMode.HTML
        )

        del posts[post_id]

    except Exception as e:
        await query.message.reply_text(
            f"❌ Не вдалося опублікувати:\n<code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML
        )


# ============================================================
# ВИБІР ВІДКЛАДЕНОГО ЧАСУ
# ============================================================

async def schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, post_id = query.data.split(":", 1)

    if post_id not in posts:
        await query.edit_message_text(
            "⚠️ Пост більше недоступний."
        )
        return

    await query.edit_message_reply_markup(
        reply_markup=time_buttons(post_id)
    )


# ============================================================
# НАЗАД ДО PREVIEW
# ============================================================

async def back_to_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, post_id = query.data.split(":", 1)

    if post_id not in posts:
        await query.edit_message_text(
            "⚠️ Пост більше недоступний."
        )
        return

    await query.edit_message_reply_markup(
        reply_markup=main_buttons(post_id)
    )


# ============================================================
# ПЛАНУВАННЯ
# ============================================================

async def choose_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # callback:
    # time|POST_ID|08:25

    parts = query.data.split("|", 2)

    if len(parts) != 3:
        return

    _, post_id, time_str = parts

    if post_id not in posts:
        await query.edit_message_text(
            "⚠️ Пост більше недоступний."
        )
        return

    hour, minute = map(int, time_str.split(":"))

    now = datetime.now(KYIV_TZ)

    target = now.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0
    )

    # Якщо час сьогодні вже минув —
    # плануємо на завтра.
    if target <= now:
        from datetime import timedelta
        target += timedelta(days=1)

    delay = (target - now).total_seconds()

    # --------------------------------------------------------
    # Створюємо job з name = post_id
    # Це потрібно для нормального скасування.
    # --------------------------------------------------------

    job = context.application.job_queue.run_once(
        scheduled_publish,
        when=delay,
        data=post_id,
        name=post_id
    )

    posts[post_id]["scheduled_job"] = job

    await query.edit_message_reply_markup(reply_markup=None)

    await query.message.reply_text(
        f"⏰ <b>Заплановано на {target.strftime('%d.%m.%Y о %H:%M')}</b>\n\n"
        "Час за Києвом.",
        parse_mode=ParseMode.HTML
    )


# ============================================================
# ЗАПУСК ЗАПЛАНОВАНОГО ПОСТА
# ============================================================

async def scheduled_publish(context: ContextTypes.DEFAULT_TYPE):
    post_id = context.job.data

    if post_id not in posts:
        return

    try:
        await publish_post(post_id, context)

        del posts[post_id]

    except Exception as e:
        print(f"Scheduled post error: {e}")


# ============================================================
# СКАСУВАННЯ
# ============================================================

async def cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, post_id = query.data.split(":", 1)

    # Якщо існує scheduled job — видаляємо його
    jobs = context.application.job_queue.get_jobs_by_name(post_id)

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
            "BOT_TOKEN не знайдено в Environment Variables."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # /start
    application.add_handler(
        CommandHandler("start", start)
    )

    # Отримання тексту / фото / відео
    application.add_handler(
        MessageHandler(
            filters.TEXT |
            filters.PHOTO |
            filters.VIDEO,
            receive_post
        )
    )

    # Кнопки
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
