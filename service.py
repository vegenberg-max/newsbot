import httpx
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

        return re.sub(
            r"bot\d+:[A-Za-z0-9_-]+",
            "bot[TOKEN HIDDEN]",
            value,
        )


def configure_logging(token):
    handler = logging.StreamHandler()
    handler.setFormatter(SecretFormatter(token))

    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        force=True,
    )

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
                "Автор старого поста не совпадает с администратором. "
                "Создайте новый пост."
            )

        if post["channel_id"] not in self.settings.channels:
            raise ContentError(
                "Сначала выберите разрешённый канал."
            )

        return prepare(post)

    async def upload_photo_preview(self, file_id):
        """
        Завантажує Telegram-фото на ImgBB і повертає
        прямий URL картинки.

        Викликається ТІЛЬКИ тоді, коли у поста немає
        готового preview_url.
        """

        if not file_id:
            return None

        if not self.settings.imgbb_api_key:
            logger.warning(
                "Photo preview required, but IMGBB_API_KEY is not configured"
            )
            return None

        try:
            # Отримуємо файл із Telegram.
            tg_file = await self.bot.get_file(file_id)

            # Завантажуємо його в пам'ять.
            photo = await tg_file.download_as_bytearray()

            # Відправляємо картинку на ImgBB.
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.imgbb.com/1/upload",
                    params={
                        "key": self.settings.imgbb_api_key,
                    },
                    files={
                        "image": (
                            "telegram.jpg",
                            bytes(photo),
                            "image/jpeg",
                        )
                    },
                )

                response.raise_for_status()
                data = response.json()

            if not data.get("success"):
                raise RuntimeError(
                    "ImgBB returned success=false"
                )

            url = data.get("data", {}).get("url")

            if not url:
                raise RuntimeError(
                    "ImgBB did not return image URL"
                )

            logger.info(
                "Telegram photo uploaded to ImgBB for link preview"
            )

            return url

        except Exception:
            # Помилка ImgBB не повинна блокувати весь пост.
            # У такому випадку пост буде відправлений без картинки.
            logger.exception(
                "Could not upload Telegram photo to ImgBB"
            )

            return None

    async def send_content(
        self,
        chat_id,
        post,
        reply_markup=None,
    ):
        text, entities = prepare(post)

        # ---------------------------------------------------------
        # 1. Спочатку беремо ВЖЕ ГОТОВЕ прев'ю.
        #
        # Якщо пересланий пост уже має нормальний preview_url,
        # ImgBB взагалі НЕ використовується.
        # ---------------------------------------------------------

        preview_url = post.get("preview_url")

        if preview_url:
            logger.info(
                "Using existing preview URL; ImgBB upload skipped"
            )

        # ---------------------------------------------------------
        # 2. ImgBB потрібен ТІЛЬКИ якщо:
        #
        #    - готового preview_url немає
        #    - але є Telegram photo_file_id
        #
        # Тобто звичайні пости з готовими прев'ю
        # через ImgBB НЕ проходять.
        # ---------------------------------------------------------

        elif post.get("photo_file_id"):
            logger.info(
                "No existing preview URL; creating preview from Telegram photo"
            )

            preview_url = await self.upload_photo_preview(
                post["photo_file_id"]
            )

        # ---------------------------------------------------------
        # 3. Якщо є URL прев'ю:
        #
        # відправляємо ЗВИЧАЙНЕ текстове повідомлення,
        # а картинку Telegram показує як link preview.
        #
        # Це дозволяє не використовувати caption фотографії.
        # ---------------------------------------------------------

        if preview_url:
            return await self.bot.send_message(
                chat_id,
                text=text,
                entities=entities,
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(
                    is_disabled=False,
                    url=preview_url,
                    show_above_text=True,
                    prefer_large_media=True,
                ),
            )

        # ---------------------------------------------------------
        # 4. Якщо:
        #
        #    - preview_url немає
        #    - Telegram-фото немає
        #
        # АБО ImgBB не зміг завантажити картинку,
        # просто публікуємо текст.
        #
        # Пост НЕ губиться.
        # ---------------------------------------------------------

        logger.info(
            "No preview available; sending text without link preview"
        )

        return await self.bot.send_message(
            chat_id,
            text=text,
            entities=entities,
            reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(
                is_disabled=True,
            ),
        )

    async def publish(
        self,
        post_id,
        revision,
        timer=False,
        allow_uncertain=False,
    ):
        post = await db_call(
            self.store.get,
            post_id,
        )

        if post["revision"] != revision:
            raise Conflict(
                "Это старое меню. "
                "Откройте пост заново через /scheduled."
            )

        try:
            self.validate(post)

        except ContentError as exc:
            if timer:
                await db_call(
                    self.store.fail_before_send,
                    post_id,
                    revision,
                    str(exc),
                )

                return await db_call(
                    self.store.get,
                    post_id,
                )

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
            sent = await self.send_content(
                claimed["channel_id"],
                claimed,
            )

            ids = [sent.message_id]
            state = "published"

        except RetryAfter as exc:
            seconds = (
                exc.retry_after.total_seconds()
                if isinstance(
                    exc.retry_after,
                    timedelta,
                )
                else float(exc.retry_after)
            )

            if claimed["attempts"] < 3:
                state = "retry"

                retry_at = (
                    int(
                        time.time()
                        + max(1, seconds)
                    )
                    + 1
                )

                error = (
                    "Telegram ограничил частоту отправки. "
                    "Бот повторит попытку автоматически."
                )

            else:
                state = "failed"

                error = (
                    "Telegram несколько раз ограничил отправку. "
                    "Пост сохранён; попробуйте позже."
                )

        except (BadRequest, Forbidden) as exc:
            state = "failed"

            # Не показуємо Telegram URL або токен
            # у повідомленні про помилку.
            detail = (
                str(exc)
                .replace(
                    self.settings.token,
                    "[скрыто]",
                )[:250]
            )

            error = (
                "Telegram отклонил публикацию. "
                "Проверьте права бота и содержимое. "
                + detail
            )

        except NetworkError:
            state = "uncertain"

            error = (
                "Нет подтверждения Telegram. "
                "Проверьте канал: пост мог уже появиться."
            )

        except TelegramError:
            state = "uncertain"

            error = (
                "Telegram не подтвердил результат. "
                "Проверьте канал перед повторной отправкой."
            )

        except Exception:
            # Неочікувана помилка після початку запиту
            # не повинна викликати автоматичне повторне
            # надсилання поста.
            logger.exception(
                "Unexpected publication error for post %s",
                post_id,
            )

            state = "uncertain"

            error = (
                "Не удалось подтвердить результат отправки. "
                "Проверьте канал."
            )

        # Спочатку зберігаємо результат публікації,
        # і тільки потім працюємо з UI/повідомленнями.
        token = claimed["attempt_token"]

        self.pending_results[token] = (
            post_id,
            token,
            state,
            error,
            ids,
            retry_at,
        )

        return await self.persist_result(
            token,
            post_id,
        )

    async def persist_result(
        self,
        token,
        post_id,
    ):
        async with self.finish_lock:
            args = self.pending_results.get(token)

            if args is None:
                return await db_call(
                    self.store.get,
                    post_id,
                )

            result = await db_call(
                self.store.finish,
                *args,
            )

            self.pending_results.pop(
                token,
                None,
            )

            return result

    async def notify(self):
        now = int(time.time())

        for post in await db_call(
            self.store.notifications,
            now,
        ):
            title = (
                post["text"]
                .replace("\n", " ")[:80]
                or "Фото/видео"
            )

            text = (
                f"{LABELS.get(post['status'], post['status'])}: "
                f"{title}"
            )

            if post["last_error"]:
                text += (
                    "\n\n"
                    + post["last_error"][:600]
                )

            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Открыть пост",
                            callback_data=(
                                f"show|{post['id']}"
                            ),
                        )
                    ]
                ]
            )

            delivered = False

            try:
                await self.bot.send_message(
                    self.settings.admin_id,
                    text=text,
                    reply_markup=markup,
                )

                delivered = True

            except TelegramError:
                logger.warning(
                    "Could not deliver notification for %s; "
                    "will retry later",
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
            # Повторюємо невдалий запис у БД,
            # але ніколи автоматично не повторюємо
            # потенційно успішне відправлення в Telegram.
            for token, result in list(
                self.pending_results.items()
            ):
                await self.persist_result(
                    token,
                    result[0],
                )

            now = int(time.time())

            await db_call(
                self.store.expire,
                now,
                self.settings.late_minutes * 60,
            )

            for post in await db_call(
                self.store.due,
                now,
            ):
                try:
                    await self.publish(
                        post["id"],
                        post["revision"],
                        timer=True,
                    )

                except (Conflict, StoreError):
                    # Адміністратор міг змінити або скасувати
                    # пост після отримання списку due.
                    continue

            await self.notify()
