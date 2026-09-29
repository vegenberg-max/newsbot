"""Durable post state and atomic transitions; no Telegram calls in transactions."""

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

EDITABLE = {"draft", "scheduled", "retry", "failed", "overdue", "needs_review"}
ACTIVE = tuple(sorted(EDITABLE | {"sending", "uncertain"}))


class StoreError(ValueError):
    pass


class Conflict(StoreError):
    pass


class Occupied(StoreError):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self, write=False):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if write:
                con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self, admin_id, channels, zone):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise StoreError(
                    "База создана более новой версией бота. Не запускайте старую версию."
                )
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            migrate = "scheduled_posts" in tables and "bot_meta" not in tables
            if migrate:
                name = self.path.with_name(
                    self.path.name + ".before-upgrade-" + uuid.uuid4().hex[:8] + ".bak"
                )
                with closing(sqlite3.connect(name)) as backup:
                    con.backup(backup)
            con.execute("PRAGMA journal_mode=WAL")
        with self.connection(write=True) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            con.execute("""CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, channel_id INTEGER,
                text TEXT NOT NULL DEFAULT '', entities_json TEXT NOT NULL DEFAULT '[]',
                preview_url TEXT, photo_file_id TEXT, video_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'draft', publish_at INTEGER, retry_at INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                attempt_token TEXT, sent_message_ids TEXT NOT NULL DEFAULT '[]', last_error TEXT,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                notification_pending INTEGER NOT NULL DEFAULT 0,
                notify_after INTEGER NOT NULL DEFAULT 0, notify_attempts INTEGER NOT NULL DEFAULT 0,
                legacy_id TEXT
            )""")
            con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_post_per_channel_minute
                ON posts(channel_id, publish_at)
                WHERE status IN ('scheduled', 'retry', 'sending') AND publish_at IS NOT NULL""")
            con.execute(
                "CREATE INDEX IF NOT EXISTS posts_queue ON posts(status, publish_at)"
            )
            if migrate:
                rows = [dict(r) for r in con.execute("SELECT * FROM scheduled_posts")]
                for old in rows:
                    now = int(time.time())
                    error = None
                    target = None
                    try:
                        dt = datetime.fromisoformat(old.get("publish_time") or "")
                        if dt.tzinfo is None:
                            raise ValueError("missing timezone")
                        target = int(dt.timestamp())
                        if target % 60:
                            raise ValueError("not an exact minute")
                    except (ValueError, TypeError):
                        error = "Проверьте дату импортированного поста и назначьте время заново."
                    owner = old.get("user_id") or admin_id
                    if owner != admin_id or old.get("channel_id") not in channels:
                        error = "В старом посте другой автор или неизвестный канал. Создайте новый пост после проверки."
                    state = "needs_review" if error else "scheduled"
                    payload = (
                        uuid.uuid4().hex,
                        owner,
                        old.get("channel_id"),
                        old.get("text") or "",
                        old.get("entities_json") or "[]",
                        old.get("preview_url"),
                        old.get("photo_file_id"),
                        old.get("video_file_id"),
                        state,
                        target,
                        error,
                        now,
                        now,
                        1 if error else 0,
                        str(old.get("post_id", "")),
                    )
                    sql = """INSERT INTO posts (id, owner_id, channel_id, text, entities_json,
                        preview_url, photo_file_id, video_file_id, status, publish_at, last_error,
                        created_at, updated_at, notification_pending, legacy_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
                    try:
                        con.execute(sql, payload)
                    except sqlite3.IntegrityError:
                        adjusted = list(payload)
                        adjusted[8] = "needs_review"
                        adjusted[10] = (
                            "В старой очереди два поста на одно время. Выберите новое время."
                        )
                        adjusted[13] = 1
                        con.execute(sql, adjusted)
                con.execute(
                    "INSERT INTO bot_meta VALUES ('legacy_imported', ?)",
                    (str(len(rows)),),
                )
            con.execute("PRAGMA user_version=1")

    @staticmethod
    def _get(con, post_id):
        row = con.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if row is None:
            raise StoreError("Пост не найден. Откройте /scheduled.")
        return dict(row)

    @staticmethod
    def _check(post, revision, states):
        if post["revision"] != revision or post["status"] not in states:
            raise Conflict(
                "Пост уже изменён или отправляется. Откройте его заново через /scheduled."
            )

    def get(self, post_id):
        with self.connection() as con:
            return self._get(con, post_id)

    def daily_posts(self, channel_id, start, end, exclude_id):
        """Отримує список запланованих постів на вибраний день."""
        with self.connection() as con:
            rows = con.execute(
                """SELECT publish_at, text FROM posts
                WHERE channel_id=? AND publish_at>=? AND publish_at<? AND id<>?
                AND status IN ('scheduled', 'retry', 'sending')
                ORDER BY publish_at""",
                (channel_id, start, end, exclude_id),
            )
            return [dict(r) for r in rows]


    def create(self, owner, payload):
        now = int(time.time())
        post_id = uuid.uuid4().hex
        with self.connection(write=True) as con:
            con.execute(
                """INSERT INTO posts
                (id, owner_id, text, entities_json, preview_url, photo_file_id, video_file_id,
                 created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post_id,
                    owner,
                    payload["text"],
                    payload["entities_json"],
                    payload.get("preview_url"),
                    payload.get("photo_file_id"),
                    payload.get("video_file_id"),
                    now,
                    now,
                ),
            )
            return self._get(con, post_id)

    def edit(self, post_id, revision, changes):
        allowed = {
            "text",
            "entities_json",
            "preview_url",
            "photo_file_id",
            "video_file_id",
            "channel_id",
        }
        if not changes or not set(changes) <= allowed:
            raise ValueError("Unknown editable field")
        with self.connection(write=True) as con:
            post = self._get(con, post_id)
            self._check(post, revision, EDITABLE)
            if "channel_id" in changes and post["status"] != "draft":
                raise Conflict("Канал можно выбирать только у нового черновика.")
            assignments = ", ".join(name + " = ?" for name in changes)
            con.execute(
                f"UPDATE posts SET {assignments}, revision = revision + 1, updated_at = ? WHERE id = ?",
                (*changes.values(), int(time.time()), post_id),
            )
            return self._get(con, post_id)

    def schedule(self, post_id, revision, target, now):
        if target <= now or target % 60:
            raise StoreError("Выберите будущую минуту публикации.")
        with self.connection(write=True) as con:
            post = self._get(con, post_id)
            self._check(post, revision, EDITABLE)
            if post["channel_id"] is None:
                raise StoreError("Сначала выберите канал.")
            try:
                con.execute(
                    """UPDATE posts SET status='scheduled', publish_at=?, retry_at=NULL,
                    attempts=0, last_error=NULL, notification_pending=0,
                    revision=revision+1, updated_at=? WHERE id=?""",
                    (target, now, post_id),
                )
            except sqlite3.IntegrityError as exc:
                raise Occupied(
                    "В этом канале уже есть пост на эту минуту. Выберите другое время."
                ) from exc
            return self._get(con, post_id)

    def cancel(self, post_id, revision):
        with self.connection(write=True) as con:
            post = self._get(con, post_id)
            self._check(post, revision, EDITABLE | {"uncertain"})
            con.execute(
                """UPDATE posts SET status='cancelled', revision=revision+1,
                updated_at=?, notification_pending=0 WHERE id=?""",
                (int(time.time()), post_id),
            )
            return self._get(con, post_id)

    def resolve_sent(self, post_id, revision):
        with self.connection(write=True) as con:
            post = self._get(con, post_id)
            self._check(post, revision, {"uncertain"})
            con.execute(
                """UPDATE posts SET status='published', revision=revision+1,
                last_error='Публикация подтверждена пользователем.', updated_at=?,
                notification_pending=0 WHERE id=?""",
                (int(time.time()), post_id),
            )
            return self._get(con, post_id)

    def claim(self, post_id, revision, now, timer=False, allow_uncertain=False):
        allowed = {"scheduled", "retry"} if timer else EDITABLE
        if allow_uncertain:
            allowed = allowed | {"uncertain"}
        with self.connection(write=True) as con:
            post = self._get(con, post_id)
            self._check(post, revision, allowed)
            due = post["retry_at"] if post["status"] == "retry" else post["publish_at"]
            if timer and (due is None or due > now):
                raise Conflict("Время публикации ещё не наступило.")
            if post["status"] == "retry" and due and due > now:
                raise Conflict(
                    "Telegram попросил подождать. Бот повторит отправку в указанное время."
                )
            token = uuid.uuid4().hex
            con.execute(
                """UPDATE posts SET status='sending', attempt_token=?, attempts=attempts+1,
                publish_at=CASE WHEN ? THEN publish_at ELSE NULL END,
                revision=revision+1, updated_at=?, notification_pending=0 WHERE id=?""",
                (token, timer, now, post_id),
            )
            return self._get(con, post_id)

    def finish(
        self, post_id, token, state, error=None, message_ids=None, retry_at=None
    ):
        if state not in {"published", "failed", "uncertain", "retry"}:
            raise ValueError("Invalid send result")
        with self.connection(write=True) as con:
            result = con.execute(
                """UPDATE posts SET status=?, last_error=?, sent_message_ids=?,
                retry_at=?, revision=revision+1, updated_at=?, notification_pending=1,
                notify_after=0, notify_attempts=0 WHERE id=? AND status='sending' AND attempt_token=?""",
                (
                    state,
                    error,
                    json.dumps(message_ids or []),
                    retry_at,
                    int(time.time()),
                    post_id,
                    token,
                ),
            )
            if result.rowcount != 1:
                raise Conflict("Результат отправки не удалось сопоставить с постом.")
            return self._get(con, post_id)

    def fail_before_send(self, post_id, revision, reason):
        with self.connection(write=True) as con:
            post = self._get(con, post_id)
            self._check(post, revision, EDITABLE)
            con.execute(
                """UPDATE posts SET status='failed', last_error=?, revision=revision+1,
                updated_at=?, notification_pending=1, notify_after=0 WHERE id=?""",
                (reason, int(time.time()), post_id),
            )

    def recover(self, now, grace):
        with self.connection(write=True) as con:
            con.execute(
                """UPDATE posts SET status='uncertain', revision=revision+1,
                last_error='Бот остановился во время отправки. Проверьте канал перед повтором.',
                updated_at=?, notification_pending=1, notify_after=0 WHERE status='sending'""",
                (now,),
            )
        self.expire(now, grace)

    def expire(self, now, grace):
        with self.connection(write=True) as con:
            con.execute(
                """UPDATE posts SET status='overdue', revision=revision+1,
                last_error='Время публикации пропущено. Выберите новое время или отправьте сейчас.',
                updated_at=?, notification_pending=1, notify_after=0
                WHERE status='scheduled' AND publish_at < ?""",
                (now, now - grace),
            )

    def due(self, now, limit=10):
        with self.connection() as con:
            return [
                dict(r)
                for r in con.execute(
                    """SELECT * FROM posts WHERE
                (status='scheduled' AND publish_at<=?) OR (status='retry' AND retry_at<=?)
                ORDER BY COALESCE(retry_at, publish_at), created_at LIMIT ?""",
                    (now, now, limit),
                )
            ]

    def page(self, page=0, history=False, size=8):
        predicate = (
            "status IN ('published', 'cancelled')"
            if history
            else "status NOT IN ('published', 'cancelled')"
        )
        with self.connection() as con:
            total = con.execute(
                f"SELECT COUNT(*) FROM posts WHERE {predicate}"
            ).fetchone()[0]
            last = max(0, (total - 1) // size)
            page = max(0, min(page, last))
            rows = con.execute(
                f"""SELECT * FROM posts WHERE {predicate}
                ORDER BY COALESCE(publish_at, created_at), created_at LIMIT ? OFFSET ?""",
                (size, page * size),
            )
            return [dict(r) for r in rows], total, page

    def occupied(self, channel_id, start, end, exclude_id):
        with self.connection() as con:
            return {
                r[0]
                for r in con.execute(
                    """SELECT publish_at FROM posts
                WHERE channel_id=? AND publish_at>=? AND publish_at<? AND id<>?
                AND status IN ('scheduled', 'retry', 'sending')""",
                    (channel_id, start, end, exclude_id),
                )
            }

    def notifications(self, now):
        with self.connection() as con:
            return [
                dict(r)
                for r in con.execute(
                    """SELECT * FROM posts WHERE
                notification_pending=1 AND notify_after<=? ORDER BY updated_at LIMIT 10""",
                    (now,),
                )
            ]

    def notification_result(self, post_id, revision, delivered, now):
        with self.connection(write=True) as con:
            if delivered:
                con.execute(
                    "UPDATE posts SET notification_pending=0 WHERE id=? AND revision=?",
                    (post_id, revision),
                )
            else:
                con.execute(
                    """UPDATE posts SET notify_after=?, notify_attempts=notify_attempts+1
                    WHERE id=? AND revision=?""",
                    (now + 300, post_id, revision),
                )
