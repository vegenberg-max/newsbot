import asyncio
import logging
import re
import time
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
)

from content import ContentError, prepare
from storage import Conflict, StoreError

logger = logging.getLogger(__name__)
LABELS = {
    "draft": "Черновик",
    "scheduled": "Запланирован",
    "retry": "Ожидает повторной попытки",
    "sending": "Отправляется",
    "uncertain": "Нужно проверить канал",
    "failed": "Ошибка",
    "overdue": "Время пропущено",
    "needs_review": "Нужна проверка",
    "published": "Опубликован",
    "cancelled": "Убран из очереди",
}


class SecretFormatter(logging.Formatter):
    def __init__(self, token):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.token = token

    def format(self, record):
        value = super().format(record)
        if self.token:
            value = value.replace(self.token, "[TOKEN HIDDEN]")
        return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[TOKEN HIDDEN]", value)


def configure_logging(token):
    handler = logging.StreamHandler()
    handler.setFormatter(SecretFormatter(token))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def db_call(method, *args, **kwargs):
    return await asyncio.to_thread(method, *args, **kwargs)


class Publisher:
    def __init__(self, store, settings, bot):
        self.store = store
        self.settings = settings
        self.bot = bot
        self.pump_lock = asyncio.Lock()
        self.finish_lock = asyncio.Lock()
        self.pending_results = {}

    def validate(self, post):
        if post["owner_id"] != self.settings.admin_id:
            raise ContentError(
                "Автор старого поста не совпадает с администратором. Создайте новый пост."
            )
        if post["channel_id"] not in self.settings.channels:
            raise ContentError("Сначала выберите разрешённый канал.")
        return prepare(post)

    async def send_content(self, chat_id, post, reply_markup=None):
        text, entities = prepare(post)
        if post.get("photo_file_id"):
            return await self.bot.send_photo(
                chat_id,
                photo=post["photo_file_id"],
                caption=text,
                caption_entities=entities,
                reply_markup=reply_markup,
            )
        if post.get("video_file_id"):
            return await self.bot.send_video(
                chat_id,
                video=post["video_file_id"],
                caption=text,
                caption_entities=entities,
                reply_markup=reply_markup,
            )
        return await self.bot.send_message(
            chat_id,
            text=text,
            entities=entities,
            reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(
                is_disabled=not bool(post.get("preview_url")),
                url=post.get("preview_url"),
                show_above_text=True,
                prefer_large_media=True,
            ),
        )

    async def publish(self, post_id, revision, timer=False, allow_uncertain=False):
        post = await db_call(self.store.get, post_id)
        if post["revision"] != revision:
            raise Conflict("Это старое меню. Откройте пост заново через /scheduled.")
        try:
            self.validate(post)
        except ContentError as exc:
            if timer:
                await db_call(self.store.fail_before_send, post_id, revision, str(exc))
                return await db_call(self.store.get, post_id)
            raise
        claimed = await db_call(
            self.store.claim,
            post_id,
            revision,
            int(time.time()),
            timer,
            allow_uncertain,
        )
        error = None
        ids = []
        retry_at = None
        try:
            sent = await self.send_content(claimed["channel_id"], claimed)
            ids = [sent.message_id]
            state = "published"
        except RetryAfter as exc:
            seconds = (
                exc.retry_after.total_seconds()
                if isinstance(exc.retry_after, timedelta)
                else float(exc.retry_after)
            )
            if claimed["attempts"] < 3:
                state = "retry"
                retry_at = int(time.time() + max(1, seconds)) + 1
                error = "Telegram ограничил частоту отправки. Бот повторит попытку автоматически."
            else:
                state = "failed"
                error = "Telegram несколько раз ограничил отправку. Пост сохранён; попробуйте позже."
        except (BadRequest, Forbidden) as exc:
            state = "failed"
            # Do not include the Telegram URL or the token in user-visible errors.
            detail = str(exc).replace(self.settings.token, "[скрыто]")[:250]
            error = (
                "Telegram отклонил публикацию. Проверьте права бота и содержимое. "
                + detail
            )
        except NetworkError:
            state = "uncertain"
            error = (
                "Нет подтверждения Telegram. Проверьте канал: пост мог уже появиться."
            )
        except TelegramError:
            state = "uncertain"
            error = "Telegram не подтвердил результат. Проверьте канал перед повторной отправкой."
        except Exception:
            # A surprising exception after initiating a request must never trigger an automatic resend.
            logger.exception("Unexpected publication error for post %s", post_id)
            state = "uncertain"
            error = "Не удалось подтвердить результат отправки. Проверьте канал."
        # Persist the domain result before touching UI or sending notifications.
        # If this write fails, the durable 'sending' record survives and recovery marks it uncertain.
        token = claimed["attempt_token"]
        self.pending_results[token] = (post_id, token, state, error, ids, retry_at)
        return await self.persist_result(token, post_id)

    async def persist_result(self, token, post_id):
        async with self.finish_lock:
            args = self.pending_results.get(token)
            if args is None:
                return await db_call(self.store.get, post_id)
            result = await db_call(self.store.finish, *args)
            self.pending_results.pop(token, None)
            return result

    async def notify(self):
        now = int(time.time())
        for post in await db_call(self.store.notifications, now):
            title = post["text"].replace("\n", " ")[:80] or "Фото/видео"
            text = f"{LABELS.get(post['status'], post['status'])}: {title}"
            if post["last_error"]:
                text += "\n\n" + post["last_error"][:600]
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Открыть пост", callback_data=f"show|{post['id']}"
                        )
                    ]
                ]
            )
            delivered = False
            try:
                await self.bot.send_message(
                    self.settings.admin_id, text=text, reply_markup=markup
                )
                delivered = True
            except TelegramError:
                logger.warning(
                    "Could not deliver notification for %s; will retry later",
                    post["id"],
                )
            await db_call(
                self.store.notification_result,
                post["id"],
                post["revision"],
                delivered,
                now,
            )

    async def tick(self, context=None):
        if self.pump_lock.locked():
            return
        async with self.pump_lock:
            # Retry a failed database write, never a potentially successful Telegram send.
            for token, result in list(self.pending_results.items()):
                await self.persist_result(token, result[0])
            now = int(time.time())
            await db_call(self.store.expire, now, self.settings.late_minutes * 60)
            for post in await db_call(self.store.due, now):
                try:
                    await self.publish(post["id"], post["revision"], timer=True)
                except (Conflict, StoreError):
                    # An administrator may edit/cancel a row after the due-list was read.
                    continue
            await self.notify()
