import os
import asyncio
from datetime import datetime, timezone
from html import escape

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

BOT_TOKEN = os.environ["BOT_TOKEN"]

# ============================================================
# НАЛАШТУВАННЯ КАНАЛІВ
# ============================================================

CHANNELS = {
    "-1004294187385": {
        "name": "🇷🇴 Новинний канал",
        "signature": (
            '<b>➡️ Більше цікавої інформації у нашому чаті</b>\n'
            '<a href="https://t.me/ua_in_ro">https://t.me/ua_in_ro</a>\n\n'
            '<b><u><a href="https://t.me/ua_in_ro">🇺🇦 Украинцы в Румынии🇹🇩</a></u></b>\n\n'
            '<b><a href="https://t.me/addlist/87244EkzpXxiZjFi">'
            '❤️ Список полезных каналов</a></b>'
        ),
    },
}

# Тимчасове сховище постів.
# Для першої версії достатньо пам'яті процесу.
posts = {}

# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привіт!\n\n"
        "Перешли мені готовий пост, який хочеш опублікувати.\n\n"
        "Після цього я запропоную:\n"
        "📢 вибрати канал\n"
        "🚀 опублікувати зараз\n"
        "⏰ відкласти публікацію"
    )


# ============================================================
# ОТРИМАННЯ ПОВІДОМЛЕННЯ
# ============================================================

async def receive_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message:
        return

    # Зберігаємо оригінальне повідомлення.
    post_id = f"{update.effective_user.id}_{message.message_id}"

    posts[post_id] = {
        "user_id": update.effective_user.id,
        "message": message,
        "chat_id": message.chat_id,
    }

    keyboard = []

    for channel_id, channel in CHANNELS.items():
        keyboard.append([
            InlineKeyboardButton(
                channel["name"],
                callback_data=f"channel:{post_id}:{channel_id}"
            )
        ])

    await message.reply_text(
        "📢 <b>Куди опублікувати?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# ВИБІР КАНАЛУ
# ============================================================

async def channel_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, post_id, channel_id = query.data.split(":", 2)

    if post_id not in posts:
        await query.edit_message_text("❌ Пост більше не знайдено.")
        return

    posts[post_id]["channel_id"] = channel_id

    channel = CHANNELS[channel_id]

    keyboard = [
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
        ],
    ]

    await query.edit_message_text(
        f"📢 <b>{escape(channel['name'])}</b>\n\n"
        "Що зробити з постом?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# ОПУБЛІКУВАТИ ЗАРАЗ
# ============================================================

async def publish_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, post_id = query.data.split(":", 1)

    post = posts.get(post_id)

    if not post:
        await query.edit_message_text("❌ Пост більше не знайдено.")
        return

    channel_id = post["channel_id"]
    message = post["message"]
    channel = CHANNELS[channel_id]

    try:
        await copy_message_with_signature(
            context,
            message,
            channel_id,
            channel["signature"],
        )

        await query.edit_message_text(
            "✅ <b>Опубліковано!</b>",
            parse_mode=ParseMode.HTML,
        )

        del posts[post_id]

    except Exception as e:
        await query.edit_message_text(
            "❌ Не вдалося опублікувати пост.\n\n"
            f"<code>{escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# ВІДКЛАСТИ
# ============================================================

async def schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, post_id = query.data.split(":", 1)

    keyboard = [
        [
            InlineKeyboardButton("08:25", callback_data=f"time:{post_id}:08:25"),
            InlineKeyboardButton("10:25", callback_data=f"time:{post_id}:10:25"),
        ],
        [
            InlineKeyboardButton("11:25", callback_data=f"time:{post_id}:11:25"),
            InlineKeyboardButton("12:25", callback_data=f"time:{post_id}:12:25"),
        ],
        [
            InlineKeyboardButton("13:25", callback_data=f"time:{post_id}:13:25"),
            InlineKeyboardButton("14:25", callback_data=f"time:{post_id}:14:25"),
        ],
        [
            InlineKeyboardButton("15:25", callback_data=f"time:{post_id}:15:25"),
            InlineKeyboardButton("16:25", callback_data=f"time:{post_id}:16:25"),
        ],
        [
            InlineKeyboardButton("17:25", callback_data=f"time:{post_id}:17:25"),
            InlineKeyboardButton("18:25", callback_data=f"time:{post_id}:18:25"),
        ],
        [
            InlineKeyboardButton("19:25", callback_data=f"time:{post_id}:19:25"),
            InlineKeyboardButton("20:25", callback_data=f"time:{post_id}:20:25"),
        ],
        [
            InlineKeyboardButton("21:25", callback_data=f"time:{post_id}:21:25"),
            InlineKeyboardButton("22:00", callback_data=f"time:{post_id}:22:00"),
        ],
        [
            InlineKeyboardButton(
                "❌ Скасувати",
                callback_data=f"cancel:{post_id}"
            )
        ],
    ]

    await query.edit_message_text(
        "⏰ <b>Вибери час публікації:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# ВИБІР ЧАСУ
# ============================================================

async def time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, post_id, hour, minute = query.data.split(":")

    post = posts.get(post_id)

    if not post:
        await query.edit_message_text("❌ Пост більше не знайдено.")
        return

    now = datetime.now()

    target = now.replace(
        hour=int(hour),
        minute=int(minute),
        second=0,
        microsecond=0,
    )

    # Якщо час сьогодні вже минув — ставимо на завтра.
    if target <= now:
        from datetime import timedelta
        target += timedelta(days=1)

    delay = (target - now).total_seconds()

    context.application.job_queue.run_once(
        scheduled_publish,
        delay,
        data={
            "post_id": post_id,
        },
    )

    post["scheduled_for"] = target

    await query.edit_message_text(
        f"✅ <b>Заплановано</b>\n\n"
        f"📅 {target.strftime('%d.%m.%Y')}\n"
        f"⏰ {target.strftime('%H:%M')}",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# ПУБЛІКАЦІЯ ЗАПЛАНОВАНОГО ПОСТА
# ============================================================

async def scheduled_publish(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    post_id = data["post_id"]

    post = posts.get(post_id)

    if not post:
        return

    channel_id = post["channel_id"]
    message = post["message"]
    channel = CHANNELS[channel_id]

    try:
        await copy_message_with_signature(
            context,
            message,
            channel_id,
            channel["signature"],
        )

        del posts[post_id]

    except Exception as e:
        print("Scheduled post error:", e)


# ============================================================
# КОПІЮВАННЯ ПОСТА + ПІДПИС
# ============================================================

async def copy_message_with_signature(
    context,
    message,
    channel_id,
    signature,
):
    # Текстові повідомлення
    if message.text:
        text = message.text + "\n\n" + signature

        await context.bot.send_message(
            chat_id=int(channel_id),
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )

    # Фото
    elif message.photo:
        caption = message.caption or ""
        caption = caption + "\n\n" + signature

        await context.bot.send_photo(
            chat_id=int(channel_id),
            photo=message.photo[-1].file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )

    # Відео
    elif message.video:
        caption = message.caption or ""
        caption = caption + "\n\n" + signature

        await context.bot.send_video(
            chat_id=int(channel_id),
            video=message.video.file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )

    # Документ
    elif message.document:
        caption = message.caption or ""
        caption = caption + "\n\n" + signature

        await context.bot.send_document(
            chat_id=int(channel_id),
            document=message.document.file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )

    else:
        raise ValueError("Цей тип повідомлення поки не підтримується.")


# ============================================================
# СКАСУВАННЯ
# ============================================================

async def cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, post_id = query.data.split(":", 1)

    # Видаляємо заплановані job-и цього поста.
    for job in context.application.job_queue.get_jobs_by_name(post_id):
        job.schedule_removal()

    posts.pop(post_id, None)

    await query.edit_message_text("❌ Скасовано.")


# ============================================================
# MAIN
# ============================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        CallbackQueryHandler(
            channel_selected,
            pattern=r"^channel:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            publish_now,
            pattern=r"^publish:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            schedule_post,
            pattern=r"^schedule:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            time_selected,
            pattern=r"^time:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            cancel_post,
            pattern=r"^cancel:"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            receive_post
        )
    )

    print("Bot started.")

    app.run_polling()


if __name__ == "__main__":
    main()
