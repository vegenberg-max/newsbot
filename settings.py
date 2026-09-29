"""Configuration without import-time side effects."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(ValueError):
    pass


DEFAULT_CHANNELS = {
    -1004294187385: "Тест",
    -1001509352451: "🇷🇴 Українці у Румунії",
}


@dataclass(frozen=True)
class Settings:
    token: str
    admin_id: int
    channels: dict[int, str]
    db_path: Path
    timezone: str = "Europe/Kyiv"
    late_minutes: int = 15

    @classmethod
    def load(cls, root: Path):
        path = root / "settings.json"
        try:
            raw = (
                json.loads(path.read_text(encoding="utf-8-sig"))
                if path.exists()
                else {}
            )
            token = os.environ.get("BOT_TOKEN", raw.get("bot_token", "")).strip()
            admin = int(os.environ.get("ADMIN_ID", raw.get("admin_id", 1598181428)))
            channels = {
                int(k): str(v) for k, v in raw.get("channels", DEFAULT_CHANNELS).items()
            }
            zone = str(raw.get("timezone", "Europe/Kyiv"))
            ZoneInfo(zone)
            late = int(raw.get("late_minutes", 15))
            db = Path(os.environ.get("DB_PATH", raw.get("db_path", "posts.db")))
        except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
            raise ConfigError(
                "Проверьте формат settings.json и переменных окружения."
            ) from exc
        if not token or token == "YOUR_BOT_TOKEN_HERE" or ":" not in token:
            raise ConfigError(
                "Укажите токен в settings.json (bot_token) или переменной BOT_TOKEN."
            )
        if (
            admin <= 0
            or not channels
            or any(k >= 0 or not v.strip() for k, v in channels.items())
        ):
            raise ConfigError(
                "Нужен положительный admin_id и непустой список каналов с отрицательными ID."
            )
        if len(channels) > 20 or any(len(name) > 80 for name in channels.values()):
            raise ConfigError(
                "Используйте до 20 каналов с названиями не длиннее 80 символов."
            )
        if not 0 <= late <= 1440:
            raise ConfigError("late_minutes должен быть от 0 до 1440.")
        if not db.is_absolute():
            db = root / db
        return cls(token, admin, channels, db.resolve(), zone, late)


class InstanceLock:
    """OS-managed lock, automatically released even if the process dies."""

    def __init__(self, database: Path):
        self.path = database.with_name(database.name + ".lock")
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise ConfigError(
                "С этой базой уже работает другой экземпляр бота. Сначала остановите его."
            ) from exc

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None
