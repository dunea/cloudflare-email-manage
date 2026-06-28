"""SQLite 自动迁移服务测试。"""

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from app.config import settings
from app.services import migration_service


def _sqlite_url(db_path: Path) -> str:
    """构造当前系统可用的 SQLite URL。"""
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def _write_alembic_version(db_path: Path, version: str) -> None:
    """创建最小 alembic_version 表并写入版本号。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)", (version,)
        )
        conn.commit()
    finally:
        conn.close()


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


def test_ensure_sqlite_schema_current_rejects_stale_db_without_auto_migrate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """已有 SQLite 版本落后且未启用自动迁移时，启动预检给出明确错误。"""
    db_path = tmp_path / "stale.db"
    _write_alembic_version(db_path, "e5f6a7b8c9d0")

    monkeypatch.setattr(settings, "AUTO_MIGRATE_SQLITE", False)
    monkeypatch.setattr(settings, "DATABASE_URL", _sqlite_url(db_path))

    with pytest.raises(RuntimeError) as exc:
        migration_service.ensure_sqlite_schema_current()

    message = str(exc.value)
    assert "SQLite 数据库迁移版本落后" in message
    assert "当前版本: e5f6a7b8c9d0" in message
    assert f"目标版本: {migration_service.get_alembic_head()}" in message
    assert str(db_path.resolve()) in message
    assert "alembic upgrade head" in message
    assert "AUTO_MIGRATE_SQLITE=true" in message


def test_ensure_sqlite_schema_current_allows_stale_db_with_auto_migrate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """启用自动迁移时，旧版本数据库交由后续 auto_migrate_sqlite 处理。"""
    db_path = tmp_path / "auto-stale.db"
    _write_alembic_version(db_path, "e5f6a7b8c9d0")

    monkeypatch.setattr(settings, "AUTO_MIGRATE_SQLITE", True)
    monkeypatch.setattr(settings, "DATABASE_URL", _sqlite_url(db_path))

    assert migration_service.ensure_sqlite_schema_current() is True


def test_ensure_sqlite_schema_current_allows_current_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """数据库版本已是当前 head 时，启动预检通过。"""
    db_path = tmp_path / "current.db"
    _write_alembic_version(db_path, migration_service.get_alembic_head())

    monkeypatch.setattr(settings, "AUTO_MIGRATE_SQLITE", False)
    monkeypatch.setattr(settings, "DATABASE_URL", _sqlite_url(db_path))

    assert migration_service.ensure_sqlite_schema_current() is True


def test_ensure_sqlite_schema_current_ignores_new_sqlite_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """新建或空 SQLite 数据库没有 alembic_version 时不误报版本落后。"""
    db_path = tmp_path / "new.db"

    monkeypatch.setattr(settings, "AUTO_MIGRATE_SQLITE", False)
    monkeypatch.setattr(settings, "DATABASE_URL", _sqlite_url(db_path))

    assert migration_service.ensure_sqlite_schema_current() is True


def test_ensure_sqlite_schema_current_reports_unreadable_sqlite_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """非空但不可读取的 SQLite 文件应明确失败，避免继续启动后变成 500。"""
    db_path = tmp_path / "broken.db"
    db_path.write_text("not a sqlite database", encoding="utf-8")

    monkeypatch.setattr(settings, "AUTO_MIGRATE_SQLITE", False)
    monkeypatch.setattr(settings, "DATABASE_URL", _sqlite_url(db_path))

    with pytest.raises(RuntimeError) as exc:
        migration_service.ensure_sqlite_schema_current()

    assert "无法读取 SQLite 数据库版本" in str(exc.value)
