"""SQLite 自动迁移服务测试。"""

from pathlib import Path

import pytest
from alembic.config import Config

from app.config import settings
from app.services import migration_service


def _sqlite_url(db_path: Path) -> str:
    """构造当前系统可用的 SQLite URL。"""
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def test_auto_migrate_sqlite_disabled_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AUTO_MIGRATE_SQLITE=false 时不执行 Alembic。"""
    monkeypatch.setattr(settings, "AUTO_MIGRATE_SQLITE", False)
    monkeypatch.setattr(settings, "DATABASE_URL", _sqlite_url(tmp_path / "app.db"))

    def _fail_upgrade(_: Config, __: str) -> None:
        raise AssertionError("不应执行迁移")

    monkeypatch.setattr(migration_service.command, "upgrade", _fail_upgrade)

    assert migration_service.auto_migrate_sqlite() is False


def test_auto_migrate_sqlite_non_sqlite_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 SQLite URL 即使开启自动迁移也跳过。"""
    monkeypatch.setattr(settings, "AUTO_MIGRATE_SQLITE", True)
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        "postgresql+asyncpg://user:pass@example.com/db",
    )

    def _fail_upgrade(_: Config, __: str) -> None:
        raise AssertionError("非 SQLite 不应执行迁移")

    monkeypatch.setattr(migration_service.command, "upgrade", _fail_upgrade)

    assert migration_service.auto_migrate_sqlite() is False


def test_ensure_sqlite_parent_dir_creates_missing_parent(tmp_path: Path) -> None:
    """文件型 SQLite 的父目录不存在时会自动创建。"""
    db_path = tmp_path / "nested" / "data" / "app.db"

    migration_service.ensure_sqlite_parent_dir(_sqlite_url(db_path))

    assert db_path.parent.is_dir()


def test_auto_migrate_sqlite_runs_alembic_for_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AUTO_MIGRATE_SQLITE=true 且 SQLite URL 时执行 upgrade head。"""
    db_path = tmp_path / "auto" / "cf_email.db"
    database_url = _sqlite_url(db_path)
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(settings, "AUTO_MIGRATE_SQLITE", True)
    monkeypatch.setattr(settings, "DATABASE_URL", database_url)

    def _fake_upgrade(config: Config, revision: str) -> None:
        calls.append((config.get_main_option("sqlalchemy.url"), revision))

    monkeypatch.setattr(migration_service.command, "upgrade", _fake_upgrade)

    assert migration_service.auto_migrate_sqlite() is True
    assert db_path.parent.is_dir()
    assert calls == [(database_url, "head")]
