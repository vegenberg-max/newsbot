"""Telegram content: preserve entities and check before attempting a send."""

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from telegram import MessageEntity


class ContentError(ValueError):
    pass


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def extract_urls(text, entities):
    found = []
    raw = text.encode("utf-16-le")
    for entity in entities or []:
        if entity.type == MessageEntity.TEXT_LINK and entity.url:
            found.append(entity.url)
        elif entity.type == MessageEntity.URL:
            found.append(
                raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode(
                    "utf-16-le"
                )
            )
    if not found:
        found = re.findall(r"https?://[^\s<>\"]+", text)
    result = []
    for value in found:
        if "://" not in value and "." in value:
            value = "https://" + value
        if urlsplit(value).scheme in {"http", "https"} and value not in result:
            result.append(value)
    return result


def final_content(text, entities):
    text = text or ""
    copied = [MessageEntity.de_json(e.to_dict()) for e in entities or []]
    original = utf16_len(text)
    raw = text.encode("utf-16-le")
    for entity in copied:
        if (
            entity.offset < 0
            or entity.length <= 0
            or entity.offset + entity.length > original
        ):
            raise ContentError("Повреждено форматирование. Отправьте текст заново.")
        try:
            raw[: entity.offset * 2].decode("utf-16-le")
            raw[: (entity.offset + entity.length) * 2].decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise ContentError(
                "Повреждено форматирование рядом с эмодзи. Отправьте текст заново."
            ) from exc
    lines = [
        "➡️ Більше цікавої інформації у нашому чаті",
        "https://t.me/ua_in_ro",
        "🇺🇦 Украинцы в Румынии 🇷🇴",
        "❤️ Список полезных каналов",
    ]
    suffix = f"\n\n{lines[0]}\n{lines[1]}\n\n{lines[2]}\n{lines[3]}"
    first = original + 2
    third = first + utf16_len(lines[0] + "\n" + lines[1] + "\n\n")
    fourth = third + utf16_len(lines[2] + "\n")
    for kind, offset, line, url in [
        (MessageEntity.BOLD, first, lines[0], None),
        (MessageEntity.BOLD, third, lines[2], None),
        (MessageEntity.UNDERLINE, third, lines[2], None),
        (MessageEntity.TEXT_LINK, third, lines[2], "https://t.me/ua_in_ro"),
        (MessageEntity.BOLD, fourth, lines[3], None),
        (
            MessageEntity.TEXT_LINK,
            fourth,
            lines[3],
            "https://t.me/addlist/87244EkzpXxiZjFi",
        ),
    ]:
        copied.append(MessageEntity(kind, offset, utf16_len(line), url=url))
    return text + suffix, copied


def entities_from_json(value):
    import json

    try:
        data = json.loads(value or "[]")
        if not isinstance(data, list):
            raise ValueError("not a list")
        for item in data:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("type"), str)
                or type(item.get("offset")) is not int
                or type(item.get("length")) is not int
            ):
                raise ValueError("invalid entity")
        return [MessageEntity.de_json(item) for item in data]
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise ContentError(
            "Не удалось прочитать сохранённое форматирование. Отредактируйте текст."
        ) from exc


def prepare(post):
    entities = entities_from_json(post["entities_json"])
    text, entities = final_content(post["text"], entities)
    limit = 1024 if post.get("photo_file_id") or post.get("video_file_id") else 4096
    if utf16_len(text) > limit:
        raise ContentError(
            f"Текст вместе с подписью длиннее лимита Telegram ({limit}). Сократите текст."
        )
    if post.get("photo_file_id") and post.get("video_file_id"):
        raise ContentError("В одном посте должно быть одно фото или одно видео.")
    return text, entities


def parse_local_time(date_text, time_text, zone, now):
    """Reject impossible/ambiguous DST times instead of silently shifting them."""
    if not re.fullmatch(r"\d{1,2}:\d{2}", time_text.strip()):
        raise ContentError("Введите время в формате ЧЧ:ММ, например 09:25.")
    try:
        day = datetime.strptime(date_text, "%Y-%m-%d").date()
        hour, minute = map(int, time_text.strip().split(":"))
        naive = datetime(day.year, day.month, day.day, hour, minute)
        tz = ZoneInfo(zone)
        candidates = set()
        for fold in (0, 1):
            aware = naive.replace(tzinfo=tz, fold=fold)
            if (
                aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
                == naive
            ):
                candidates.add(int(aware.timestamp()))
    except ValueError as exc:
        raise ContentError("Проверьте дату и время: часы 00–23, минуты 00–59.") from exc
    if len(candidates) != 1:
        raise ContentError(
            "Это время пропущено или повторяется при переводе часов. Выберите другое."
        )
    target = candidates.pop()
    if target <= now:
        raise ContentError("Это время уже прошло. Выберите будущее время.")
    return target
