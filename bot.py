"""Private Telegram publishing assistant. Run with: python bot.py"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import (
    CallbackQuery,
    InlineKeyboardButton as Button,
    InlineKeyboardMarkup as Markup,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from content import ContentError, extract_urls, parse_local_time, prepare
from service import LABELS, Publisher, configure_logging, db_call
from settings import ConfigError, InstanceLock, Settings
from storage import EDITABLE, Conflict, Store, StoreError

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
TIMES = (
    "08:25",
    "08:55",
    "09:25",
    "09:35",
    "09:56",
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
)


def service(context):
    return context.application.bot_data["publisher"]


def authorized(update, context):
    user, chat = update.effective_user, update.effective_chat
    return bool(
        user
        and chat
        and chat.type == "private"
        and user.id == service(context).settings.admin_id
    )


def action(name, post, extra=None):
    value = f"{name}|{post['id']}|{post['revision']}"
    if extra is not None:
        value += "|" + str(extra)
    if len(value.encode("utf-8")) > 64:
        raise ValueError("Callback data is too long")
    return value


async def present(target, text, keyboard=None):
    markup = Markup(keyboard) if keyboard is not None else None
    if isinstance(target, CallbackQuery):
        if target.message:
            try:
                if target.message.caption is not None:
                    await target.message.edit_caption(caption=text, reply_markup=markup)
                else:
                    await target.message.edit_text(text, reply_markup=markup)
                return
            except BadRequest as exc:
                detail = str(exc).lower()
                if "message is not modified" in detail:
                    return
                if not any(
                    s in detail
                    for s in (
                        "message to edit not found",
                        "message can't be edited",
                        "message can not be edited",
                    )
                ):
                    pass
        if target.message:
            await target.message.reply_text(text, reply_markup=markup)
    else:
        await target.reply_text(text, reply_markup=markup)


def local_time(value, settings):
    if value is None:
        return "не вказано"
    return datetime.fromtimestamp(value, ZoneInfo(settings.timezone)).strftime(
        "%d.%m.%Y %H:%M"
    )


async def controls(target, post, context):
    pub = service(context)
    config = pub.settings
    own = post["owner_id"] == config.admin_id
    
    rows = []
    # Якщо канал ще не обрано — пропонуємо вибір каналу
    if post["status"] == "draft" and post["channel_id"] is None:
        for index, name in enumerate(config.channels.values()):
            rows.append([Button(name, callback_data=action("ch", post, index))])
    elif post["status"] in EDITABLE and own:
        rows += [
            [Button("🚀 Опублікувати зараз", callback_data=action("send", post))],
            [Button("🗓 Вибрати дату і час", callback_data=action("plan", post))],
            [
                Button("✏️ Текст", callback_data=action("edit", post)),
                Button("🖼 Фото/відео", callback_data=action("media", post)),
            ],
        ]
        if post["text"]:
            rows.append([Button("🗑 Видалити текст", callback_data=action("clear", post))])
        if post["photo_file_id"] or post["video_file_id"]:
            rows.append([Button("🗑 Видалити фото/відео", callback_data=action("nomed", post))])

    if post["status"] in EDITABLE | {"uncertain"}:
        rows.append([Button("🗑 Видалити з черги", callback_data=action("del", post))])
    rows.append(
        [
            Button("📋 Черга", callback_data="list|0"),
            Button("📜 Історія", callback_data="hist|0"),
        ]
    )

    markup = Markup(rows)

    # Якщо викликано з CallbackQuery (кліку по кнопці) — видаляємо старе повідомлення календаря/меню, 
    # щоб прикріпити чистий предпросмотр з кнопками знизу
    if isinstance(target, CallbackQuery):
        try:
            await target.message.delete()
        except TelegramError:
            pass
        chat_id = target.message.chat_id
    else:
        chat_id = target.chat_id

    await pub.send_content(chat_id, post, reply_markup=markup)


async def listing(target, context, page=0, history=False):
    pub = service(context)
    posts, total, page = await db_call(pub.store.page, page, history)
    text = ("📜 Історія" if history else "📋 Черга та чернетки") + f" · всього {total}\n"
    rows = []
    for item in posts:
        title = item["text"].replace("\n", " ")[:50] or "Фото/відео"
        channel = pub.settings.channels.get(item["channel_id"], "Канал не обрано")
        text += f"\n{title}\n{channel} · {LABELS.get(item['status'], item['status'])}"
        if item["publish_at"]:
            text += " · " + local_time(item["publish_at"], pub.settings)
        text += "\n"
        rows.append([Button(title, callback_data=f"show|{item['id']}")])
    prefix = "hist" if history else "list"
    nav = []
    if page:
        nav.append(Button("←", callback_data=f"{prefix}|{page - 1}"))
    if (page + 1) * 8 < total:
        nav.append(Button("→", callback_data=f"{prefix}|{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            Button(
                "📋 Черга" if history else "📜 Історія",
                callback_data="list|0" if history else "hist|0",
            )
        ]
    )
    if not total:
        text += "\nТут поки порожньо. Надішліть боту текст, одне фото або одне відео."
    await present(target, text, rows)


async def calendar(target, post, context, day=None):
    pub = service(context)
    pub.validate(post)
    tz = ZoneInfo(pub.settings.timezone)
    today = datetime.now(tz).date()
    
    if day is None:
        selected = today
    else:
        try:
            selected = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ContentError("Введіть дату у форматі РРРР-ММ-ДД.") from exc
            
    if selected < today or selected > today + timedelta(days=366):
        raise ContentError("Оберіть дату від сьогоднішнього дня до року вперед.")
        
    day = selected.isoformat()
    midnight = datetime.combine(selected, datetime.min.time(), tzinfo=tz)
    
    daily_items = await db_call(
        pub.store.daily_posts,
        post["channel_id"],
        int(midnight.timestamp()),
        int((midnight + timedelta(days=1)).timestamp()),
        post["id"],
    )
    occupied = {item["publish_at"] for item in daily_items}

    text = f"⏰ Час публікації ({selected.strftime('%d.%m.%Y')})\n\n"
    if daily_items:
        text += "Заплановані пости на цей день:\n"
        for item in daily_items:
            t_str = datetime.fromtimestamp(item["publish_at"], tz).strftime("%H:%M")
            title = (item["text"] or "Фото/відео").replace("\n", " ")[:35]
            text += f"📌 {t_str} — {title}...\n"
        text += "\n"
        
    text += "Оберіть час з меню або введіть його текстом:"

    rows, nav = [], []
    if selected > today:
        nav.append(Button("← День", callback_data=action("day", post, (selected - timedelta(days=1)).isoformat())))
    if selected < today + timedelta(days=366):
        nav.append(Button("День →", callback_data=action("day", post, (selected + timedelta(days=1)).isoformat())))
    if nav:
        rows.append(nav)

    current = []
    for value in TIMES:
        try:
            stamp = parse_local_time(day, value, pub.settings.timezone, int(time.time()))
        except ContentError:
            continue
        if stamp in occupied:
            current.append(Button("Зайнято " + value, callback_data="busy"))
        else:
            data = day.replace("-", "") + value.replace(":", "")
            current.append(Button(value, callback_data=action("time", post, data)))
        if len(current) == 3:
            rows.append(current)
            current = []
    if current:
        rows.append(current)

    rows += [
        [Button("⌨️ Ввести час", callback_data=action("clock", post, day))],
        [Button("⌨️ Ввести дату", callback_data=action("date", post))],
        [Button("⬅️ Назад до поста", callback_data=f"show|{post['id']}")],
    ]
    await present(target, text, rows)


async def start(update, context):
    if not authorized(update, context):
        if update.effective_chat and update.effective_chat.type == "private":
            await update.effective_message.reply_text(
                f"Доступ лише в адміністратора. Ваш Telegram ID: {update.effective_user.id}"
            )
        return
    context.user_data.pop("await", None)
    await update.effective_message.reply_text(
        "Надішліть текст, одне фото або одне відео. Я збережу чернетку і запропоную канал.\n\n"
        "/scheduled — черга та чернетки\n/history — опубліковані та скасовані\n"
        "/cancel — скасувати поточне введення\n/id — ваш Telegram ID\n\n"
        "Перед відправкою перевірте попередній перегляд. Час вказано за часовим поясом "
        + service(context).settings.timezone
        + "."
    )


async def identity(update, context):
    if (
        update.effective_user
        and update.effective_chat
        and update.effective_chat.type == "private"
    ):
        await update.effective_message.reply_text(
            f"Ваш Telegram ID: {update.effective_user.id}"
        )


async def list_command(update, context):
    if authorized(update, context):
        context.user_data.pop("await", None)
        await listing(update.effective_message, context)


async def history_command(update, context):
    if authorized(update, context):
        context.user_data.pop("await", None)
        await listing(update.effective_message, context, history=True)


async def cancel_input(update, context):
    if authorized(update, context):
        context.user_data.pop("await", None)
        await update.effective_message.reply_text(
            "Введення скасовано. Пости збережено. Відкрити чергу: /scheduled."
        )


def payload(message):
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    urls = extract_urls(text, entities)
    preview = urls[0] if urls else None
    options = message.link_preview_options
    if options:
        if options.is_disabled:
            preview = None
        elif options.url and options.url.startswith(("https://", "http://")):
            preview = options.url
    return {
        "text": text,
        "entities_json": json.dumps(
            [e.to_dict() for e in entities], ensure_ascii=False
        ),
        "photo_file_id": message.photo[-1].file_id if message.photo else None,
        "video_file_id": message.video.file_id if message.video else None,
        "preview_url": preview,
    }


async def receive(update, context):
    if not authorized(update, context):
        return
    message = update.effective_message
    pub = service(context)
    if message.media_group_id:
        groups = context.user_data.setdefault("rejected_albums", [])
        if message.media_group_id not in groups:
            groups.append(message.media_group_id)
            del groups[:-50]
            await message.reply_text(
                "Це альбом з кількох файлів. Надішліть потрібне фото або відео окремо: альбом цілком поки не підтримується. Нічого з альбому не поставлено в чергу."
            )
        return
    pending = context.user_data.get("await")
    if pending and pending["expires"] < time.time():
        context.user_data.pop("await", None)
        await message.reply_text(
            "Час введення вичерпано. Відкрийте пост через /scheduled і оберіть дію ще раз."
        )
        return
    try:
        if pending:
            post = await db_call(pub.store.get, pending["id"])
            if (
                post["revision"] != pending["revision"]
                or post["status"] not in EDITABLE
            ):
                context.user_data.pop("await", None)
                raise Conflict("Пост змінився. Відкрийте його заново через /scheduled.")
            kind = pending["kind"]
            if kind in {"clock", "date"}:
                if not message.text:
                    raise ContentError("Надішліть дату або час звичайним текстом.")
                if kind == "date":
                    await calendar(message, post, context, message.text.strip())
                else:
                    target = parse_local_time(
                        pending["date"],
                        message.text,
                        pub.settings.timezone,
                        int(time.time()),
                    )
                    pub.validate(post)
                    post = await db_call(
                        pub.store.schedule,
                        post["id"],
                        post["revision"],
                        target,
                        int(time.time()),
                    )
                    await controls(message, post, context)
                context.user_data.pop("await", None)
                return
            if kind == "edit":
                if not message.text:
                    raise ContentError(
                        "Надішліть новий текст. Для видалення тексту скористайтеся кнопкою «Видалити текст»."
                    )
                new = payload(message)
                changes = {
                    key: new[key] for key in ("text", "entities_json", "preview_url")
                }
            elif kind == "media":
                if not message.photo and not message.video:
                    raise ContentError("Надішліть одне фото або одне відео.")
                changes = {
                    "photo_file_id": message.photo[-1].file_id
                    if message.photo
                    else None,
                    "video_file_id": message.video.file_id if message.video else None,
                }
            else:
                raise ContentError("Відкрийте пост заново через /scheduled.")
            prepare({**post, **changes})
            post = await db_call(pub.store.edit, post["id"], post["revision"], changes)
            context.user_data.pop("await", None)
            await controls(message, post, context)
            return
        if not (message.text or message.photo or message.video):
            raise ContentError("Підтримуються текст, одне фото або одне відео.")
        new = payload(message)
        prepare(new)
        post = await db_call(pub.store.create, pub.settings.admin_id, new)
        await controls(message, post, context)
    except (ContentError, StoreError) as exc:
        await message.reply_text(str(exc) + "\n/cancel — скасувати введення.")


async def callbacks(update, context):
    query = update.callback_query
    if not query:
        return
    if not authorized(update, context):
        try:
            await query.answer("Немає доступу.", show_alert=True)
        except TelegramError:
            pass
        return
    try:
        await query.answer(
            "Ця хвилина зайнята у вибраному каналі." if query.data == "busy" else None
        )
    except TelegramError:
        logger.info("Could not acknowledge callback")
    context.user_data.pop("await", None)
    pub = service(context)
    try:
        parts = (query.data or "").split("|")
        name = parts[0]
        if name == "busy":
            return
        if name in {"list", "hist"} and len(parts) == 2:
            await listing(query, context, max(0, int(parts[1])), name == "hist")
            return
        if name == "show" and len(parts) == 2:
            await controls(query, await db_call(pub.store.get, parts[1]), context)
            return
        if len(parts) not in {3, 4}:
            raise ContentError("Це меню старої версії. Відкрийте /scheduled.")
        post = await db_call(pub.store.get, parts[1])
        revision = int(parts[2])
        if post["revision"] != revision:
            raise Conflict(
                "Це старе меню. Відкрийте актуальний пост через /scheduled."
            )
        if name == "view":
            await pub.send_content(pub.settings.admin_id, post)
        elif name == "ch" and len(parts) == 4:
            index = int(parts[3])
            channels = list(pub.settings.channels)
            if not 0 <= index < len(channels):
                raise ContentError("Канал не знайдено.")
            post = await db_call(
                pub.store.edit, post["id"], revision, {"channel_id": channels[index]}
            )
            await controls(query, post, context)
        elif name in {"send", "yesag"}:
            post = await pub.publish(
                post["id"], revision, allow_uncertain=name == "yesag"
            )
            await controls(query, post, context)
        elif name in {"plan", "day"}:
            if post["status"] not in EDITABLE:
                raise Conflict("Цей пост зараз не можна перенести.")
            await calendar(
                query,
                post,
                context,
                parts[3] if name == "day" and len(parts) == 4 else None,
            )
        elif name == "time" and len(parts) == 4:
            value = parts[3]
            if len(value) != 12 or not value.isdigit():
                raise ContentError(
                    "Пошкоджено кнопку часу. Відкрийте календар заново."
                )
            day = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
            target = parse_local_time(
                day,
                value[8:10] + ":" + value[10:12],
                pub.settings.timezone,
                int(time.time()),
            )
            pub.validate(post)
            post = await db_call(
                pub.store.schedule, post["id"], revision, target, int(time.time())
            )
            await controls(query, post, context)
        elif name in {"edit", "media", "clock", "date"}:
            if (
                post["status"] not in EDITABLE
                or post["owner_id"] != pub.settings.admin_id
            ):
                raise Conflict("Цей пост зараз не можна редагувати.")
            if name in {"clock", "date"}:
                pub.validate(post)
            if name == "clock" and len(parts) != 4:
                raise ContentError("Відкрийте календар заново.")
            context.user_data["await"] = {
                "kind": name,
                "id": post["id"],
                "revision": revision,
                "date": parts[3] if name == "clock" else None,
                "expires": time.time() + 900,
            }
            prompts = {
                "edit": "Надішліть новий текст.",
                "media": "Надішліть нове фото або відео.",
                "clock": "Введіть час, наприклад 09:25.",
                "date": "Введіть дату, наприклад 2026-12-15 (РРРР-ММ-ДД).",
            }
            await query.message.reply_text(prompts[name] + "\n/cancel — скасувати введення.")
        elif name in {"clear", "nomed"}:
            changes = (
                {"text": "", "entities_json": "[]", "preview_url": None}
                if name == "clear"
                else {"photo_file_id": None, "video_file_id": None}
            )
            prepare({**post, **changes})
            await controls(
                query,
                await db_call(pub.store.edit, post["id"], revision, changes),
                context,
            )
        elif name == "del":
            await present(
                query,
                "Видалити цей пост з черги? Повідомлення, які вже з'явилися в каналі, залишаться там.",
                [
                    [Button("Так, видалити", callback_data=action("yesdel", post))],
                    [Button("Назад", callback_data=f"show|{post['id']}")],
                ],
            )
        elif name == "yesdel":
            await controls(
                query, await db_call(pub.store.cancel, post["id"], revision), context
            )
        elif name == "done":
            await controls(
                query,
                await db_call(pub.store.resolve_sent, post["id"], revision),
                context,
            )
        elif name == "again":
            if post["status"] != "uncertain":
                raise Conflict("Відкрийте пост заново.")
            await present(
                query,
                "Надіслати ще раз? Натискайте лише якщо перевірили канал і поста там немає.",
                [
                    [
                        Button(
                            "Перевірив: поста немає, надіслати",
                            callback_data=action("yesag", post),
                        )
                    ],
                    [Button("Назад", callback_data=f"show|{post['id']}")],
                ],
            )
        else:
            raise ContentError("Кнопка застаріла. Відкрийте /scheduled.")
    except (ContentError, StoreError) as exc:
        await query.message.reply_text(str(exc))
    except (ValueError, IndexError):
        await query.message.reply_text(
            "Не вдалося прочитати кнопку. Відкрийте /scheduled заново."
        )


async def error_handler(update, context):
    logger.error(
        "Handler failed",
        exc_info=(type(context.error), context.error, context.error.__traceback__),
    )
    if (
        isinstance(update, Update)
        and authorized(update, context)
        and update.effective_message
    ):
        try:
            await update.effective_message.reply_text(
                "Не вдалося завершити дію або оновити меню. Відкрийте /scheduled і перевірте стан поста перед повтором."
            )
        except TelegramError:
            pass


async def unknown_command(update, context):
    if authorized(update, context):
        await update.effective_message.reply_text(
            "Невідома команда. Допомога: /start."
        )


async def post_init(application):
    pub = application.bot_data["publisher"]
    await db_call(pub.store.recover, int(time.time()), pub.settings.late_minutes * 60)
    application.job_queue.run_repeating(
        pub.tick,
        interval=5,
        first=1,
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
    )
    try:
        await application.bot.send_message(
            pub.settings.admin_id,
            "Бот запущено. Чернетки та черга: /scheduled. Якщо бот ще не спілкувався з вами, натисніть /start.",
        )
    except TelegramError:
        logger.warning(
            "Administrator notification unavailable; user may need to press /start"
        )


def build_application(config, store):
    application = (
        Application.builder()
        .token(config.token)
        .post_init(post_init)
        .connect_timeout(10)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .concurrent_updates(False)
        .build()
    )
    application.bot_data["publisher"] = Publisher(store, config, application.bot)
    for name, handler in [
        ("start", start),
        ("help", start),
        ("id", identity),
        ("scheduled", list_command),
        ("history", history_command),
        ("cancel", cancel_input),
    ]:
        application.add_handler(
            CommandHandler(
                name,
                handler,
                filters=filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE,
            )
        )
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & filters.COMMAND,
            unknown_command,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & ~filters.COMMAND,
            receive,
        )
    )
    application.add_error_handler(error_handler)
    return application


def main():
    lock = None
    try:
        config = Settings.load(ROOT)
        configure_logging(config.token)
        lock = InstanceLock(config.db_path)
        lock.acquire()
        store = Store(config.db_path)
        store.initialize(config.admin_id, config.channels, config.timezone)
        app = build_application(config, store)
        app.run_polling(
            allowed_updates=["message", "callback_query"], drop_pending_updates=False
        )
    except (ConfigError, StoreError) as exc:
        print(f"Бот не запущено: {exc}")
        raise SystemExit(1) from exc
    except Exception:
        logger.exception("Bot stopped because of an error")
        print(
            "Бот зупинено. Перевірте налаштування, доступ до бази та повідомлення про помилку вище."
        )
        raise SystemExit(1) from None
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    main()
