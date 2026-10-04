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
    db_url: str
    db_token: str
    imgbb_api_key: str = ""
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

            token = os.environ.get(
                "BOT_TOKEN",
                raw.get("bot_token", ""),
            ).strip()

            admin = int(
                os.environ.get(
                    "ADMIN_ID",
                    raw.get("admin_id", 1598181428),
                )
            )

            channels = {
                int(k): str(v)
                for k, v in raw.get(
                    "channels",
                    DEFAULT_CHANNELS,
                ).items()
            }

            zone = str(
                raw.get(
                    "timezone",
                    "Europe/Kyiv",
                )
            )

            ZoneInfo(zone)

            late = int(
                raw.get(
                    "late_minutes",
                    15,
                )
            )

            # Turso
            db_url = os.environ.get(
                "DB_URL",
                raw.get("db_url", "posts.db"),
            ).strip()

            db_token = os.environ.get(
                "DB_TOKEN",
                raw.get("db_token", ""),
            ).strip()

            # ImgBB
            imgbb_api_key = os.environ.get(
                "IMGBB_API_KEY",
                raw.get("imgbb_api_key", ""),
            ).strip()

        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            AttributeError,
        ) as exc:
            raise ConfigError(
                "Проверьте формат settings.json "
                "и переменных окружения."
            ) from exc

        if (
            not token
            or token == "YOUR_BOT_TOKEN_HERE"
            or ":" not in token
        ):
            raise ConfigError(
                "Укажите токен в settings.json (bot_token) "
                "или переменной BOT_TOKEN."
            )

        if (
            admin <= 0
            or not channels
            or any(
                k >= 0 or not v.strip()
                for k, v in channels.items()
            )
        ):
            raise ConfigError(
                "Нужен положительный admin_id "
                "и непустой список каналов "
                "с отрицательными ID."
            )

        if (
            len(channels) > 20
            or any(
                len(name) > 80
                for name in channels.values()
            )
        ):
            raise ConfigError(
                "Используйте до 20 каналов "
                "с названиями не длиннее 80 символов."
            )

        if not 0 <= late <= 1440:
            raise ConfigError(
                "late_minutes должен быть от 0 до 1440."
            )

        return cls(
            token=token,
            admin_id=admin,
            channels=channels,
            db_url=db_url,
            db_token=db_token,
            imgbb_api_key=imgbb_api_key,
            timezone=zone,
            late_minutes=late,
        )


class InstanceLock:
    """Для хмарної бази Turso локальне блокування файлу не потрібне."""

    def __init__(self, database=None):
        pass

    def acquire(self):
        pass

    def close(self):
        pass
