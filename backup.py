"""Make a consistent SQLite backup, including committed data still in the WAL."""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from settings import Settings


def backup_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError("База не найдена. Проверьте db_path / DB_PATH.")
    if source == destination or destination.exists():
        raise ValueError("Копия уже существует. Выберите новое имя.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Read-only mode avoids accidentally creating an empty source database.
    with closing(
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)
    ) as original:
        with closing(sqlite3.connect(destination)) as copied:
            original.backup(copied)
            check = copied.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise sqlite3.DatabaseError("Проверка резервной копии не пройдена.")
    return destination


def main():
    try:
        config = Settings.load(Path(__file__).resolve().parent)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = (
            config.db_path.parent / "backups" / f"posts-{stamp}-{uuid4().hex[:6]}.db"
        )
        print("Резервная копия сохранена:", backup_database(config.db_path, target))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("Не удалось создать копию:", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
