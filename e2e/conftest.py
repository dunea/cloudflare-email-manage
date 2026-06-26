"""e2e 测试夹具：临时 SQLite + 假 CF 模式 + 后台 uvicorn 真实服务。

运行：``.venv/Scripts/python.exe -m pytest e2e/``（需先 ``playwright install chromium``）。
默认 ``pytest``（pyproject 中 testpaths=["tests"]）不会收集本目录，互不影响。
"""

import os
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# —— 必须在导入 app 之前设置环境变量（app.config.settings 在导入时读取）——
_TMPDIR = Path(tempfile.mkdtemp(prefix="cfem-e2e-"))
_DB_PATH = _TMPDIR / "e2e.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH.as_posix()}"
os.environ["SECRET_KEY"] = "e2e-secret-key-at-least-32-characters!!"
os.environ["CF_FAKE_MODE"] = "1"
os.environ["CF_WEBHOOK_SECRET"] = "e2e-webhook-secret"
os.environ["ADMIN_EMAIL"] = "admin@example.com"
os.environ["ADMIN_PASSWORD"] = "adminpass123"
os.environ["COOKIE_SECURE"] = "false"

from app.services.cloudflare import _reset_fake_destination_addresses  # noqa: E402


@pytest.fixture(autouse=True)
def reset_fake_cloudflare_state() -> Iterator[None]:
    """每个 e2e 用例前后清理假 CF 内存状态。"""
    _reset_fake_destination_addresses()
    yield
    _reset_fake_destination_addresses()


def _free_port() -> int:
    """获取一个可用端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def live_server() -> Iterator[str]:
    """后台线程启动真实 uvicorn 服务，返回 base_url。"""
    import uvicorn
    from sqlalchemy import create_engine

    import app.models  # noqa: F401  确保所有表注册到 Base.metadata
    from app.database import Base
    from app.main import app

    # 用同步引擎在临时库建表（e2e 临时库，直接 create_all，不走 alembic）
    sync_engine = create_engine(f"sqlite:///{_DB_PATH.as_posix()}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    ready = False
    while time.time() < deadline:
        if server.started:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    ready = True
                    break
            except OSError:
                pass
        time.sleep(0.1)
    if not ready:
        raise RuntimeError("e2e 服务启动超时")

    yield base_url

    server.should_exit = True
    thread.join(timeout=10)
