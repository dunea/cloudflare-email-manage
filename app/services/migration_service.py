"""启动期数据库迁移辅助逻辑。"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from alembic import command
from app.config import settings


def is_sqlite_database_url(database_url: str) -> bool:
    """判断数据库 URL 是否为 SQLite。"""
    try:
        url = make_url(database_url)
    except ArgumentError:
        return False
    return url.drivername.split("+", 1)[0] == "sqlite"


def ensure_sqlite_parent_dir(database_url: str) -> None:
    """为文件型 SQLite 数据库创建父目录。"""
    try:
        url = make_url(database_url)
    except ArgumentError:
        return
    if url.drivername.split("+", 1)[0] != "sqlite":
        return

    database = url.database
    if not database or database == ":memory:":
        return

    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def run_alembic_upgrade_head() -> None:
    """执行 Alembic upgrade head。"""
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    command.upgrade(config, "head")


def auto_migrate_sqlite() -> bool:
    """按配置自动迁移 SQLite 数据库，返回是否实际执行迁移。"""
    if not settings.AUTO_MIGRATE_SQLITE:
        return False
    if not is_sqlite_database_url(settings.DATABASE_URL):
        return False

    ensure_sqlite_parent_dir(settings.DATABASE_URL)
    run_alembic_upgrade_head()
    return True
