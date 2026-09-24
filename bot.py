import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
KYIV = ZoneInfo("Europe/Kyiv")
# ============================================================
# КАНАЛИ
# ============================================================
CHANNELS = {
    "-1004294187385": {
        "name": "🇷🇴 Тестовий канал",
        "signature_text": (
            "➡️ Більше цікавої інформації у нашому чаті\n"
            "https://t.me/ua_in_ro\n\n"
            "🇺🇦 Украинцы в Румынии🇹🇩\n"
            "❤️ Список полезных каналов"
        ),
    }
}
# ============================================================
# ПОСТИ
# ============================================================
posts = {}
# ============================================================
# UTF-16
# ============================================================
def utf16_length(text: str) -> int:
    """
    Telegram використовує UTF-16 offsets
    для MessageEntity.
    """
    return len(text.encode("utf-16-le")) // 2
# ============================================================
# ENTITY HELPERS
# ============================================================
def copy_entities(entities):
    """
    Копіює Telegram MessageEntity у словники.
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
    Відновлює MessageEntity.
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
# ============================================================
# ПІДПИС
# ============================================================
def build_signature(channel):
    """
    Створює текст підпису та правильні UTF-16 entities.
    """
    line1 = "➡️ Більше цікавої інформації у нашому чаті"
    url_line = "https://t.me/ua_in_ro"
    line3 = "🇺🇦 Украинцы в Румынии🇹🇩"
    line4 = "❤️ Список полезных каналов"
    signature_text = (
        f"{line1}\n"
        f"{url_line}\n"
        f"\n"
        f"{line3}\n"
        f"{line4}"
    )
    # Початок "Украинцы..."
    line3_offset = utf16_length(
        line1 + "\n" +
        url_line + "\n\n"
    )
    # Початок "Список..."
    line4_offset = (
        line3_offset +
        utf16_length(line3 + "\n")
    )
    entities = [
        # Перший рядок — жирний
        MessageEntity(
            type=MessageEntityType.BOLD,
            offset=0,
            length=utf16_length(line1),
        ),
        # Украинцы — жирний
        MessageEntity(
            type=MessageEntityType.BOLD,
            offset=line3_offset,
            length=utf16_length(line3),
        ),
        # Украинцы — підкреслений
        MessageEntity(
            type=MessageEntityType.UNDERLINE,
            offset=line3_offset,
            length=utf16_length(line3),
        ),
        # Украинцы — посилання
        MessageEntity(
            type=MessageEntityType.TEXT_LINK,
            offset=line3_offset,
            length=utf16_length(line3),
            url="https://t.me/ua_in_ro",
        ),
        # Список — жирний
        MessageEntity(
            type=MessageEntityType.BOLD,
            offset=line4_offset,
            length=utf16_length(line4),
        ),
        # Список — посилання
        MessageEntity(
            type=MessageEntityType.TEXT_LINK,
            offset=line4_offset,
            length=utf16_length(line4),
            url="https://t.me/addlist/87244EkzpXxiZjFi",
        ),
    ]
    return signature_text, entities
# ============================================================
# ФІНАЛЬНИЙ ТЕКСТ
# ============================================================
def build_final_content(post):
    original_text = post.get("text") or ""
    original_entities = post.get("entities") or []
    channel = CHANNELS[post["channel_id"]]
    signature_text, signature_entities = (
        build_signature(channel)
    )
    if original_text:
        final_text = (
            original_text +
            "\n\n" +
            signature_text
        )
        signature_offset = utf16_length(
            original_text + "\n\n"
        )
    else:
        final_text = signature_text
        signature_offset = 0
    final_entities = []
    # Оригінальні entities
    for entity in original_entities:
        final_entities.append(
            make_entity(entity)
        )
    # Entities підпису
    for entity in signature_entities:
        final_entities.append(
            MessageEntity(
                type=entity.type,
                offset=(
                    entity.offset +
                    signature_offset
                ),
                length=entity.length,
                url=entity.url,
                user=entity.user,
                language=entity.language,
                custom_emoji_id=entity.custom_emoji_id,
            )
        )
    return final_text, final_entities
# ============================================================
# КНОПКИ
# ============================================================
def channel_buttons(post_id):
    rows = []
    for channel_id, channel in CHANNELS.items():
        rows.append([
            InlineKeyboardButton(
                channel["name"],
                callback_data=(
                    f"channel:{post_id}:{channel_id}"
                ),
            )
        ])
    rows.append([
        InlineKeyboardButton(
            "❌ Скасувати",
            callback_data=f"cancel:{post_id}",
        )
    ])
    return InlineKeyboardMarkup(rows)
def action_buttons(post_id):
    return InlineKeyboardMarkup([
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
    for time_value in times:
        row.append(
            InlineKeyboardButton(
                time_value,
                callback_data=(
                    f"time|{post_id}|{time_value}"
                ),
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
            callback_data=f"back:{post_id}",
        )
    ])
    return InlineKeyboardMarkup(rows)
# ============================================================
# START
# ============================================================
async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "👋 <b>Привіт!</b>\n\n"
        "Перешли мені готовий пост.\n\n"
        "Я покажу його готовий вигляд "
        "перед публікацією.",
        parse_mode="HTML",
    )
# ============================================================
# ОТРИМАННЯ ПОСТА
# ============================================================
async def receive_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message
    if not message:
        return
    # Працюємо тільки в особистому чаті
    if update.effective_chat.type != "private":
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
            "⚠️ Цей тип повідомлення "
            "поки не підтримується."
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
    await message.reply_text(
        "📢 <b>Куди публікуємо?</b>",
        parse_mode="HTML",
        reply_markup=channel_buttons(post_id),
    )
# ============================================================
# PREVIEW
# ============================================================
async def send_preview(
    chat_id,
    post_id,
    context
):
    if post_id not in posts:
        raise RuntimeError("Пост більше не знайдений.")
    post = posts[post_id]
    if not post.get("channel_id"):
        raise RuntimeError("Канал не вибраний.")
    final_text, final_entities = (
        build_final_content(post)
    )
    # --------------------------------------------------------
    # ФОТО
    # --------------------------------------------------------
    if post["photo_file_id"]:
        return await context.bot.send_photo(
            chat_id=chat_id,
            photo=post["photo_file_id"],
            caption=final_text,
            caption_entities=final_entities,
            reply_markup=action_buttons(post_id),
        )
    # --------------------------------------------------------
    # ВІДЕО
    # --------------------------------------------------------
    if post["video_file_id"]:
        return await context.bot.send_video(
            chat_id=chat_id,
            video=post["video_file_id"],
            caption=final_text,
            caption_entities=final_entities,
            reply_markup=action_buttons(post_id),
        )
    # --------------------------------------------------------
    # ТЕКСТ
    # --------------------------------------------------------
    return await context.bot.send_message(
        chat_id=chat_id,
        text=final_text,
        entities=final_entities,
        disable_web_page_preview=False,
        reply_markup=action_buttons(post_id),
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
    try:
        _, post_id, channel_id = (
            query.data.split(":", 2)
        )
    except ValueError:
        return
    if post_id not in posts:
        await query.edit_message_text(
            "⚠️ Пост більше недоступний."
        )
        return
    posts[post_id]["channel_id"] = channel_id
    try:
        # Спочатку створюємо preview.
        # Видаляємо меню тільки після успіху.
        await send_preview(
            query.message.chat_id,
            post_id,
            context,
        )
        await query.message.delete()
    except Exception as e:
        print(
            "BOT ERROR while creating preview:",
            repr(e),
        )
        try:
            await query.edit_message_text(
                "❌ Не вдалося створити preview.\n\n"
                f"{e}"
            )
        except Exception:
            pass
# ============================================================
# ПУБЛІКАЦІЯ
# ============================================================
async def publish_post(
    post_id,
    context
):
    if post_id not in posts:
        raise RuntimeError("Пост більше не знайдений.")
    post = posts[post_id]
    channel_id = post.get("channel_id")
    if not channel_id:
        raise RuntimeError("Канал не вибраний.")
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
            caption_entities=final_entities,
        )
        return
    # --------------------------------------------------------
    # ВІДЕО
    # --------------------------------------------------------
    if post["video_file_id"]:
        await context.bot.send_video(
            chat_id=channel_id,
            video=post["video_file_id"],
            caption=final_text,
            caption_entities=final_entities,
        )
        return
    # --------------------------------------------------------
    # ТЕКСТ
    # --------------------------------------------------------
    await context.bot.send_message(
        chat_id=channel_id,
        text=final_text,
        entities=final_entities,
        disable_web_page_preview=False,
    )
# ============================================================
# ОПУБЛІКУВАТИ ЗАРАЗ
# ============================================================
async def publish_now(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer("Публікую…")
    _, post_id = query.data.split(":", 1)
    if post_id not in posts:
        await query.answer(
            "Пост більше недоступний.",
            show_alert=True,
        )
        return
    try:
        await publish_post(
            post_id,
            context,
        )
        posts.pop(post_id, None)
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✅ Опубліковано!",
        )
    except Exception as e:
        print(
            "BOT ERROR while publishing:",
            repr(e),
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "❌ Не вдалося опублікувати пост.\n\n"
                f"{e}"
            ),
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
    _, post_id = query.data.split(":", 1)
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
    _, post_id = query.data.split(":", 1)
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
    try:
        _, post_id, time_string = (
            query.data.split("|", 2)
        )
    except ValueError:
        return
    if post_id not in posts:
        return
    hour, minute = map(
        int,
        time_string.split(":"),
    )
    now = datetime.now(KYIV)
    target = now.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    delay = (
        target - now
    ).total_seconds()
    # Якщо старий job існує — видаляємо
    old_jobs = (
        context.application
        .job_queue
        .get_jobs_by_name(post_id)
    )
    for job in old_jobs:
        job.schedule_removal()
    context.application.job_queue.run_once(
        scheduled_publish,
        when=delay,
        data=post_id,
        name=post_id,
    )
    await query.edit_message_reply_markup(
        reply_markup=None
    )
    await query.message.reply_text(
        "⏰ <b>Заплановано!</b>\n\n"
        f"📅 {target.strftime('%d.%m.%Y')}\n"
        f"🕐 {target.strftime('%H:%M')}\n\n"
        "Час за Києвом.",
        parse_mode="HTML",
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
            context,
        )
        posts.pop(post_id, None)
        print(
            f"Scheduled post published: {post_id}"
        )
    except Exception as e:
        print(
            "BOT ERROR while scheduled publishing:",
            repr(e),
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
    _, post_id = query.data.split(":", 1)
    old_jobs = (
        context.application
        .job_queue
        .get_jobs_by_name(post_id)
    )
    for job in old_jobs:
        job.schedule_removal()
    posts.pop(post_id, None)
    try:
        await query.message.delete()
    except Exception:
        try:
            await query.edit_message_text(
                "❌ Скасовано."
            )
        except Exception:
            pass
# ============================================================
# ERROR HANDLER
# ============================================================
async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    print(
        "BOT ERROR:",
        repr(context.error),
    )
# ============================================================
# MAIN — WEBHOOK
# ============================================================
def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не знайдено в Render Environment."
        )
    if not RENDER_EXTERNAL_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL не знайдено."
        )
    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/telegram"
    )
    print("🤖 Bot starting in WEBHOOK mode")
    print(
        "🌐 Webhook:",
        webhook_url
    )
    print(
        "🔌 Port:",
        PORT
    )
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
            start,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            select_channel,
            pattern=r"^channel:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            publish_now,
            pattern=r"^publish:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            schedule_post,
            pattern=r"^schedule:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            choose_time,
            pattern=r"^time\|",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            back_to_preview,
            pattern=r"^back:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cancel_post,
            pattern=r"^cancel:",
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT |
            filters.PHOTO |
            filters.VIDEO,
            receive_post,
        )
    )
    application.add_error_handler(
        error_handler
    )
    # --------------------------------------------------------
    # WEBHOOK
    # --------------------------------------------------------
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
# ============================================================
# START
# ============================================================
if __name__ == "__main__":
    main()
