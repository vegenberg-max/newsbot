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
    "10:25",
    "11:25",
    "12:25",
    "13:25",
    "14:25",
    "15:25",
    "16:25",
    "17:25",
    "18:17",
    "19:25",
    "20:25",
    "21:25",
    "22:00",
)

ALT_TIMES = (
    "07:55",
    "08:55",
    "09:55",
    "10:55",
    "11:55",
    "12:55",
    "13:55",
    "14:55",
    "15:55",
    "16:55",
    "17:55",
    "18:55",
    "19:55",
    "20:55",
    "21:55",
    "22:55",
)

DAYS_UA = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
MONTHS_UA = [
    "січ", "лют", "берез", "квіт", "трав", "черв",
    "лип", "серп", "верес", "жовт", "лист", "груд",
]

ACTIVE_TTL = 30 * 60
SESSION_LIMIT = 20


def format_date_ua(dt):
    return f"{DAYS_UA[dt.weekday()]}, {dt.day} {MONTHS_UA[dt.month - 1]}"


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
                    await target.edit_message_caption(caption=text, reply_markup=markup)
                    return
                await target.edit_message_text(text=text, reply_markup=markup)
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


# ---------------------------------------------------------------------------
# Робоча сесія постів: 1 / 2 / 3
# ---------------------------------------------------------------------------

def clear_work_session(context):
    context.user_data.pop("work_posts", None)
    context.user_data.pop("active_post", None)
    context.user_data.pop("work_expires", None)


def normalize_work_session(context):
    expires = context.user_data.get("work_expires")
    if expires is not None and expires < time.time():
        clear_work_session(context)
        return []

    posts = context.user_data.setdefault("work_posts", [])
    posts = list(dict.fromkeys(posts))

    if len(posts) > SESSION_LIMIT:
        posts = posts[-SESSION_LIMIT:]

    context.user_data["work_posts"] = posts

    if context.user_data.get("active_post") not in posts:
        context.user_data["active_post"] = posts[-1] if posts else None

    return posts


def touch_work_session(context):
    context.user_data["work_expires"] = time.time() + ACTIVE_TTL


def add_work_post(context, post_id, make_active=True):
    posts = normalize_work_session(context)

    if post_id not in posts:
        posts.append(post_id)

    if len(posts) > SESSION_LIMIT:
        posts[:] = posts[-SESSION_LIMIT:]

    context.user_data["work_posts"] = posts

    if make_active:
        context.user_data["active_post"] = post_id

    touch_work_session(context)


def active_post_id(context):
    normalize_work_session(context)
    return context.user_data.get("active_post")


def remove_work_post(context, post_id):
    posts = normalize_work_session(context)

    if post_id in posts:
        posts.remove(post_id)

    context.user_data["work_posts"] = posts

    if context.user_data.get("active_post") == post_id:
        context.user_data["active_post"] = posts[-1] if posts else None

    if posts:
        touch_work_session(context)
    else:
        clear_work_session(context)


def forwarded_message(message):
    return bool(
        getattr(message, "forward_origin", None)
        or getattr(message, "forward_date", None)
        or getattr(message, "forward_from", None)
        or getattr(message, "forward_from_chat", None)
        or getattr(message, "forward_sender_name", None)
    )


async def work_switch_rows(context):
    pub = service(context)
    ids = normalize_work_session(context)

    if not ids:
        return []

    active = active_post_id(context)
    buttons = []
    valid_ids = []

    for post_id in ids:
        try:
            post = await db_call(pub.store.get, post_id)
        except StoreError:
            continue

        valid_ids.append(post_id)
        number = len(valid_ids)

        if post["status"] in {"scheduled", "retry"}:
            marker = " ✅"
        elif post["status"] in {"failed", "overdue", "needs_review", "uncertain"}:
            marker = " ⚠️"
        elif post["status"] == "published":
            marker = " ✓"
        elif post["status"] == "cancelled":
            marker = " ×"
        else:
            marker = " ✏️"

        prefix = "● " if post_id == active else ""
        buttons.append(
            Button(f"{prefix}{number}{marker}", callback_data=f"work|{post_id}")
        )

    context.user_data["work_posts"] = valid_ids

    if active not in valid_ids:
        context.user_data["active_post"] = valid_ids[-1] if valid_ids else None

    return [buttons[i:i + 5] for i in range(0, len(buttons), 5)]


async def controls(target, post, context):
    pub = service(context)
    config = pub.settings
    own = post["owner_id"] == config.admin_id

    add_work_post(context, post["id"], make_active=True)
    rows = await work_switch_rows(context)

    if post["status"] == "draft" and post["channel_id"] is None:
        for index, name in enumerate(config.channels.values()):
            rows.append([Button(name, callback_data=action("ch", post, index))])

    elif post["status"] in EDITABLE and own:
        plan_label = (
            "🗓 Змінити дату і час"
            if post["publish_at"]
            else "🗓 Вибрати дату і час"
        )

        rows += [
            [Button("🚀 Опублікувати зараз", callback_data=action("send", post))],
            [Button(plan_label, callback_data=action("plan", post))],
        ]

        if post["text"]:
            rows.append(
                [Button("🗑 Видалити текст", callback_data=action("clear", post))]
            )

        if post["photo_file_id"] or post["video_file_id"]:
            rows.append(
                [
                    Button(
                        "🗑 Видалити фото/відео",
                        callback_data=action("nomed", post),
                    )
                ]
            )

    if post["status"] == "uncertain" and own:
        rows += [
            [
                Button(
                    "✅ Пост уже є в каналі",
                    callback_data=action("done", post),
                )
            ],
            [
                Button(
                    "🔁 Надіслати ще раз",
                    callback_data=action("again", post),
                )
            ],
        ]

    if post["status"] in EDITABLE | {"uncertain"}:
        rows.append(
            [Button("🗑 Видалити з черги", callback_data=action("del", post))]
        )

    if post["status"] in EDITABLE and own:
        rows.append([Button("✖️ Готово", callback_data="workdone")])

    rows.append(
        [
            Button("📋 Черга", callback_data="list|0"),
            Button("📜 Історія", callback_data="history"),
        ]
    )

    markup = Markup(rows)

    if isinstance(target, CallbackQuery):
        chat_id = target.message.chat_id
        try:
            await target.message.delete()
        except TelegramError:
            pass
    else:
        chat_id = target.chat_id

    await pub.send_content(chat_id, post, reply_markup=markup)


# ---------------------------------------------------------------------------
# Черга та історія
# ---------------------------------------------------------------------------

async def listing(target, context, page=0):
    pub = service(context)
    posts, total, page = await db_call(pub.store.page, page, False)

    text = f"📋 Черга та чернетки · всього {total}\n"
    rows = []

    for item in posts:
        title = item["text"].replace("\n", " ")[:50] or "Фото/відео"
        channel = pub.settings.channels.get(item["channel_id"], "Канал не обрано")

        text += f"\n{title}\n{channel} · {LABELS.get(item['status'], item['status'])}"

        if item["publish_at"]:
            text += " · " + local_time(item["publish_at"], pub.settings)

        text += "\n"
        rows.append([Button(title, callback_data=f"show|{item['id']}")])

    nav = []

    if page:
        nav.append(Button("←", callback_data=f"list|{page - 1}"))

    if (page + 1) * 8 < total:
        nav.append(Button("→", callback_data=f"list|{page + 1}"))

    if nav:
        rows.append(nav)

    rows.append([Button("📜 Історія", callback_data="history")])

    if not total:
        text += (
            "\nТут поки порожньо. "
            "Надішліть боту текст, одне фото або одне відео."
        )

    await present(target, text, rows)


async def history_menu(target, context):
    pub = service(context)
    counts = await db_call(pub.store.publication_counts, int(time.time()))

    rows = [
        [
            Button(
                f"✅ Опубліковані · {counts['published']}",
                callback_data="pubs|0",
            )
        ],
        [
            Button(
                f"⚠️ Не опубліковані · {counts['failed']}",
                callback_data="fails|0",
            )
        ],
        [
            Button(
                f"🗓 Заплановані · {counts['scheduled']}",
                callback_data="plans|0",
            )
        ],
        [Button("📋 Черга та чернетки", callback_data="list|0")],
    ]

    await present(target, "📜 Публікації\n\nОберіть розділ:", rows)


async def publication_listing(target, context, kind, page=0):
    pub = service(context)

    if kind == "published":
        posts, total, page = await db_call(pub.store.published_page, page, 10)
        heading = "✅ Опубліковані"
        prefix = "pubs"

    elif kind == "failed":
        posts, total, page = await db_call(pub.store.failed_page, page, 10)
        heading = "⚠️ Не опубліковані"
        prefix = "fails"

    elif kind == "scheduled":
        posts, total, page = await db_call(
            pub.store.scheduled_page,
            page,
            10,
            int(time.time()),
        )
        heading = "🗓 Заплановані"
        prefix = "plans"

    else:
        raise ContentError("Невідомий розділ історії.")

    text = f"{heading} · всього {total}\n"
    rows = []

    for item in posts:
        title = item["text"].replace("\n", " ")[:50] or "Фото/відео"
        channel = pub.settings.channels.get(item["channel_id"], "Канал не обрано")

        text += f"\n{title}\n{channel} · {LABELS.get(item['status'], item['status'])}"

        if item["status"] == "retry" and item.get("retry_at"):
            when = item["retry_at"]
        elif item.get("publish_at"):
            when = item["publish_at"]
        else:
            when = item.get("updated_at")

        if when:
            text += " · " + local_time(when, pub.settings)

        if kind == "failed" and item.get("last_error"):
            error = item["last_error"].replace("\n", " ")[:90]
            text += f"\n⚠️ {error}"

        text += "\n"
        rows.append([Button(title, callback_data=f"show|{item['id']}")])

    nav = []

    if page:
        nav.append(Button("←", callback_data=f"{prefix}|{page - 1}"))

    if (page + 1) * 10 < total:
        nav.append(Button("→", callback_data=f"{prefix}|{page + 1}"))

    if nav:
        rows.append(nav)

    rows.append([Button("← Публікації", callback_data="history")])

    if not total:
        if kind == "published":
            text += "\nОпублікованих постів поки немає."
        elif kind == "failed":
            text += "\nПроблемних постів немає."
        else:
            text += "\nМайбутніх публікацій немає."

    await present(target, text, rows)


# ---------------------------------------------------------------------------
# Календар
# ---------------------------------------------------------------------------

async def get_daily_items(pub, post, start_ts, end_ts):
    """Отримує всі відкладені пости на день. Якщо канал не обрано — опитує всі канали."""

    if post.get("channel_id"):
        return await db_call(
            pub.store.daily_posts,
            post["channel_id"],
            start_ts,
            end_ts,
            post["id"],
        )

    all_items = []

    for ch_id in pub.settings.channels.keys():
        items = await db_call(
            pub.store.daily_posts,
            ch_id,
            start_ts,
            end_ts,
            post["id"],
        )
        all_items.extend(items)

    all_items.sort(key=lambda x: x["publish_at"])
    return all_items


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

    day_str = selected.isoformat()
    midnight = datetime.combine(selected, datetime.min.time(), tzinfo=tz)
    start_ts = int(midnight.timestamp())
    end_ts = int((midnight + timedelta(days=1)).timestamp())

    daily_items = await get_daily_items(pub, post, start_ts, end_ts)
    occupied = {item["publish_at"] for item in daily_items}
    date_ua = format_date_ua(selected)

    text = "⏰ Час публікації\n\n"

    if daily_items:
        text += f"Заплановані пости на {date_ua}:\n"

        for item in daily_items:
            t_str = datetime.fromtimestamp(item["publish_at"], tz).strftime("%H:%M")
            title = (item["text"] or "Фото/відео").replace("\n", " ")[:35]
            text += f"📍 {t_str} — {title}...\n"

        text += "\n"

    text += "Оберіть час з меню або надішліть його текстом:"

    rows = []
    nav = []

    prev_day = selected - timedelta(days=1)
    next_day = selected + timedelta(days=1)

    if selected > today:
        nav.append(
            Button(
                "←",
                callback_data=action("day", post, prev_day.isoformat()),
            )
        )
    else:
        nav.append(Button(" ", callback_data="busy"))

    nav.append(Button(f"🗓 {date_ua}", callback_data="busy"))

    if selected < today + timedelta(days=366):
        nav.append(
            Button(
                "→",
                callback_data=action("day", post, next_day.isoformat()),
            )
        )

    rows.append(nav)

    current = []

    for value in TIMES:
        try:
            stamp = parse_local_time(
                day_str,
                value,
                pub.settings.timezone,
                int(time.time()),
            )
        except ContentError:
            continue

        if stamp in occupied:
            continue

        data = day_str.replace("-", "") + value.replace(":", "")
        current.append(
            Button(
                value,
                callback_data=action("time", post, data),
            )
        )

        if len(current) == 3:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    rows += [
        [
            Button(
                "✍️ Вибрати годину та хвилини",
                callback_data=action("clock", post, day_str),
            )
        ],
        [Button("← Назад", callback_data=f"show|{post['id']}")],
    ]

    await present(target, text, rows)


# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------

async def start(update, context):
    if not authorized(update, context):
        if update.effective_chat and update.effective_chat.type == "private":
            await update.effective_message.reply_text(
                f"Доступ лише в адміністратора. Ваш Telegram ID: {update.effective_user.id}"
            )
        return

    context.user_data.pop("await", None)

    await update.effective_message.reply_text(
        "Надішліть або перешліть пост — я збережу його.\n\n"
        "Якщо переслати кілька постів поспіль, вони з'являться "
        "у швидкому перемикачі 1 · 2 · 3.\n"
        "Натисніть номер, щоб працювати з потрібним постом.\n\n"
        "Коли пост активний:\n"
        "• звичайний текст замінює його текст;\n"
        "• нове фото замінює картинку;\n"
        "• фото з підписом замінює картинку і текст;\n"
        "• переслане повідомлення створює НОВИЙ пост.\n\n"
        "/scheduled — черга та чернетки\n"
        "/history — публікації\n"
        "/cancel — скасувати поточне введення\n"
        "/id — ваш Telegram ID\n\n"
        "Час вказано за часовим поясом "
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
        await history_menu(update.effective_message, context)


async def cancel_input(update, context):
    if authorized(update, context):
        context.user_data.pop("await", None)
        await update.effective_message.reply_text(
            "Введення скасовано. Пости збережено. Відкрити чергу: /scheduled."
        )


# ---------------------------------------------------------------------------
# Вміст повідомлення
# ---------------------------------------------------------------------------

def payload(message):
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    urls = extract_urls(text, entities)

    external_urls = [
        url
        for url in urls
        if not any(
            domain in url.lower()
            for domain in ("t.me", "telegram.me", "telegram.dog")
        )
    ]

    preview = external_urls[0] if external_urls else (urls[0] if urls else None)

    options = message.link_preview_options

    if options:
        if options.is_disabled:
            preview = None

        elif options.url and options.url.startswith(("https://", "http://")):
            if (
                any(
                    domain in options.url.lower()
                    for domain in ("t.me", "telegram.me", "telegram.dog")
                )
                and external_urls
            ):
                preview = external_urls[0]
            else:
                preview = options.url

    return {
        "text": text,
        "entities_json": json.dumps(
            [entity.to_dict() for entity in entities],
            ensure_ascii=False,
        ),
        "photo_file_id": message.photo[-1].file_id if message.photo else None,
        "video_file_id": message.video.file_id if message.video else None,
        "preview_url": preview,
    }


async def create_new_post(message, context):
    pub = service(context)
    new = payload(message)

    prepare(new)

    post = await db_call(
        pub.store.create,
        pub.settings.admin_id,
        new,
    )

    add_work_post(context, post["id"], make_active=True)
    await controls(message, post, context)

    return post


async def edit_active_post(message, context, post):
    pub = service(context)

    if post["status"] not in EDITABLE or post["owner_id"] != pub.settings.admin_id:
        raise Conflict("Цей пост зараз не можна редагувати.")

    # Нове фото.
    # Спочатку отримуємо постійний ImgBB URL.
    # Якщо завантаження не вдалося, БД не змінюється.
    if message.photo:
        file_id = message.photo[-1].file_id
        preview_url = await pub.save_photo_preview(file_id)

        changes = {
            "preview_url": preview_url,
            "photo_file_id": file_id,
            "video_file_id": None,
        }

        if message.caption is not None:
            new = payload(message)
            changes["text"] = new["text"]
            changes["entities_json"] = new["entities_json"]

        prepare({**post, **changes})

        updated = await db_call(
            pub.store.edit,
            post["id"],
            post["revision"],
            changes,
        )

        add_work_post(context, updated["id"], make_active=True)
        await controls(message, updated, context)
        return updated

    # Відео.
    if message.video:
        changes = {
            "preview_url": None,
            "photo_file_id": None,
            "video_file_id": message.video.file_id,
        }

        if message.caption is not None:
            new = payload(message)
            changes["text"] = new["text"]
            changes["entities_json"] = new["entities_json"]

        prepare({**post, **changes})

        updated = await db_call(
            pub.store.edit,
            post["id"],
            post["revision"],
            changes,
        )

        add_work_post(context, updated["id"], make_active=True)
        await controls(message, updated, context)
        return updated

    # Звичайний текст.
    if message.text:
        new = payload(message)

        changes = {
            "text": new["text"],
            "entities_json": new["entities_json"],
        }

        # Якщо власного фото/відео немає, link preview
        # повинен відповідати саме новому тексту.
        if not post["photo_file_id"] and not post["video_file_id"]:
            changes["preview_url"] = new["preview_url"]

        prepare({**post, **changes})

        updated = await db_call(
            pub.store.edit,
            post["id"],
            post["revision"],
            changes,
        )

        add_work_post(context, updated["id"], make_active=True)
        await controls(message, updated, context)
        return updated

    raise ContentError("Надішліть текст, одне фото або одне відео.")


# ---------------------------------------------------------------------------
# Отримання повідомлень
# ---------------------------------------------------------------------------

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
                "Це альбом з кількох файлів. Надішліть потрібне фото або відео "
                "окремо: альбом цілком поки не підтримується. "
                "Нічого з альбому не поставлено в чергу."
            )

        return

    pending = context.user_data.get("await")

    if pending and pending["expires"] < time.time():
        context.user_data.pop("await", None)

        await message.reply_text(
            "Час введення вичерпано. Відкрийте пост через /scheduled "
            "і оберіть дію ще раз."
        )
        return

    try:
        # Очікування часу/дати має пріоритет над активним постом.
        if pending:
            post = await db_call(pub.store.get, pending["id"])

            if (
                post["revision"] != pending["revision"]
                or post["status"] not in EDITABLE
            ):
                context.user_data.pop("await", None)
                raise Conflict(
                    "Пост змінився. Відкрийте його заново через /scheduled."
                )

            kind = pending["kind"]

            if kind in {"clock", "date"}:
                if not message.text:
                    raise ContentError("Надішліть дату або час звичайним текстом.")

                if kind == "date":
                    context.user_data.pop("await", None)
                    await calendar(message, post, context, message.text.strip())
                    return

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

                context.user_data.pop("await", None)
                add_work_post(context, post["id"], make_active=True)

                tz = ZoneInfo(pub.settings.timezone)
                dt = datetime.fromtimestamp(target, tz)

                await message.reply_text(
                    f"✅ ⏰ Пост заплановано на "
                    f"{dt.strftime('%d.%m.%Y')} о {dt.strftime('%H:%M')}"
                )

                await controls(message, post, context)
                return

            context.user_data.pop("await", None)
            raise ContentError("Відкрийте пост заново через /scheduled.")

        # Переслане повідомлення ЗАВЖДИ додається як новий пост.
        if forwarded_message(message):
            await create_new_post(message, context)
            return

        # Звичайний текст/фото/відео редагує активний пост.
        post_id = active_post_id(context)

        if post_id:
            try:
                post = await db_call(pub.store.get, post_id)
            except StoreError:
                remove_work_post(context, post_id)
                post = None

            if (
                post
                and post["status"] in EDITABLE
                and post["owner_id"] == pub.settings.admin_id
            ):
                await edit_active_post(message, context, post)
                return

        # Якщо активного поста немає — створюємо новий.
        if not (message.text or message.photo or message.video):
            raise ContentError("Підтримуються текст, одне фото або одне відео.")

        await create_new_post(message, context)

    except (ContentError, StoreError) as exc:
        await message.reply_text(str(exc) + "\n/cancel — скасувати введення.")


# ---------------------------------------------------------------------------
# Callback-кнопки
# ---------------------------------------------------------------------------

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
            "Ця хвилина зайнята у вибраному каналі."
            if query.data == "busy"
            else None
        )
    except TelegramError:
        logger.info("Could not acknowledge callback")

    pub = service(context)

    try:
        parts = (query.data or "").split("|")
        name = parts[0]

        if name == "busy":
            return

        # Закриває тільки швидку робочу сесію.
        # Пости з бази не видаляються.
        if name == "workdone":
            context.user_data.pop("await", None)
            clear_work_session(context)

            await present(
                query,
                "✅ Редагування закрито.\n\n"
                "Усі пости збережені. Відкрити їх можна через "
                "📋 Чергу або 📜 Історію.",
                [
                    [
                        Button("📋 Черга", callback_data="list|0"),
                        Button("📜 Історія", callback_data="history"),
                    ]
                ],
            )
            return

        # Перемикання 1 / 2 / 3.
        if name == "work" and len(parts) == 2:
            context.user_data.pop("await", None)

            post = await db_call(pub.store.get, parts[1])
            add_work_post(context, post["id"], make_active=True)

            await controls(query, post, context)
            return

        if name == "list" and len(parts) == 2:
            context.user_data.pop("await", None)
            await listing(query, context, max(0, int(parts[1])))
            return

        # hist|0 залишено для сумісності зі старими повідомленнями бота.
        if name == "history" or (name == "hist" and len(parts) == 2):
            context.user_data.pop("await", None)
            await history_menu(query, context)
            return

        if name == "pubs" and len(parts) == 2:
            context.user_data.pop("await", None)
            await publication_listing(
                query,
                context,
                "published",
                max(0, int(parts[1])),
            )
            return

        if name == "fails" and len(parts) == 2:
            context.user_data.pop("await", None)
            await publication_listing(
                query,
                context,
                "
