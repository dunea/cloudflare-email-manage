"""启动期数据库迁移辅助逻辑。"""

import sqlite3
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from alembic import command
from app.config import settings


def sqlite_database_path(database_url: str) -> Path | None:
    """获取文件型 SQLite 数据库路径，内存库或非 SQLite 返回 None。"""
    try:
        url = make_url(database_url)
    except ArgumentError:
        return None
    if url.drivername.split("+", 1)[0] != "sqlite":
        return None

    database = url.database
    if not database or database == ":memory:":
        return None

    return Path(database).expanduser()


def is_sqlite_database_url(database_url: str) -> bool:
    """判断数据库 URL 是否为 SQLite。"""
    return sqlite_database_path(database_url) is not None


def ensure_sqlite_parent_dir(database_url: str) -> None:
    """为文件型 SQLite 数据库创建父目录。"""
    db_path = sqlite_database_path(database_url)
    if db_path is None:
        return

    db_path.parent.mkdir(parents=True, exist_ok=True)


def _alembic_config() -> Config:
    """构造使用当前应用数据库 URL 的 Alembic 配置。"""
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


def get_alembic_head() -> str:
    """读取当前代码对应的 Alembic head revision。"""
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("未找到 Alembic head revision")
    return head


def get_sqlite_alembic_version(database_url: str) -> str | None:
    """只读读取 SQLite 数据库中的 alembic_version。"""
    db_path = sqlite_database_path(database_url)
    if db_path is None or not db_path.exists() or db_path.stat().st_size == 0:
        return None

    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"无法读取 SQLite 数据库版本: {db_path.resolve()} ({exc})"
        ) from exc

    try:
        has_version_table = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'alembic_version'
            """
        ).fetchone()
        if has_version_table is None:
            return None

        row = conn.execute(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"无法读取 SQLite 数据库版本: {db_path.resolve()} ({exc})"
        ) from exc
    finally:
        conn.close()

    if row is None or row[0] is None:
        return None
    return str(row[0])


def ensure_sqlite_schema_current() -> bool:
    """启动前检查已迁移 SQLite 数据库是否落后于当前代码。"""
    db_path = sqlite_database_path(settings.DATABASE_URL)
    if db_path is None:
        return True

    current_version = get_sqlite_alembic_version(settings.DATABASE_URL)
    if current_version is None:
        return True

    target_version = get_alembic_head()
    if current_version == target_version:
        return True

    if settings.AUTO_MIGRATE_SQLITE:
        return True

    db_display_path = str(db_path.resolve())
    raise RuntimeError(
        "SQLite 数据库迁移版本落后，应用已拒绝启动以避免运行期 500。\n"
        f"当前版本: {current_version}\n"
        f"目标版本: {target_version}\n"
        f"数据库路径: {db_display_path}\n"
        "请先备份数据库，然后执行 "
        ".\\.venv\\Scripts\\python.exe -m alembic upgrade head；"
        "或在确认可自动迁移时设置 AUTO_MIGRATE_SQLITE=true。"
    )


def run_alembic_upgrade_head() -> None:
    """执行 Alembic upgrade head。"""
    command.upgrade(_alembic_config(), "head")


def auto_migrate_sqlite() -> bool:
    """按配置自动迁移 SQLite 数据库，返回是否实际执行迁移。"""
    if not settings.AUTO_MIGRATE_SQLITE:
        return False
    if not is_sqlite_database_url(settings.DATABASE_URL):
        return False

    ensure_sqlite_parent_dir(settings.DATABASE_URL)
    run_alembic_upgrade_head()
    return True
