"""配置读取（环境变量 / .env）。

约束：不得引入 python-dotenv 等额外依赖，这里用极简的手写 .env 解析。
优先级：已存在的进程环境变量 > .env 文件。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# backend/app/config.py -> backend/app -> backend -> 项目根
APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent


def _candidate_env_files() -> list[Path]:
    return [
        PROJECT_ROOT / ".env",
        BACKEND_DIR / ".env",
        Path.cwd() / ".env",
    ]


def load_dotenv() -> str | None:
    """把找到的第一个 .env 载入环境变量（不覆盖已有值）。返回所用路径。"""
    for path in _candidate_env_files():
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
            return str(path)
        except OSError:
            continue
    return None


ENV_FILE = load_dotenv()


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


class Settings:
    """运行时配置快照。"""

    def __init__(self) -> None:
        self.newapi_base_url = str(_get("NEWAPI_BASE_URL", "http://127.0.0.1:3000")).rstrip("/")
        self.newapi_admin_user = str(_get("NEWAPI_ADMIN_USER", "LzK"))
        self.newapi_admin_password = str(_get("NEWAPI_ADMIN_PASSWORD", ""))
        self.panel_user = str(_get("PANEL_USER", "LzK"))
        self.panel_password = str(_get("PANEL_PASSWORD", ""))
        self.panel_session_secret = str(_get("PANEL_SESSION_SECRET", "insecure-dev-secret"))
        self.listen_port = int(_get("LISTEN_PORT", "8080") or "8080")
        # 对 New API 的请求超时（秒）
        self.request_timeout = float(_get("NEWAPI_TIMEOUT", "10") or "10")
        # 面板 session 有效期：7 天，滑动续期
        self.session_ttl = 7 * 24 * 3600
        # cookie 是否要求 https；本机 http 联调默认 false，生产走 Caddy(=TLS) 可置 true
        self.cookie_secure = str(_get("PANEL_COOKIE_SECURE", "false")).lower() in ("1", "true", "yes", "on")
        self.version = "0.1.0"
        self.frontend_dir = PROJECT_ROOT / "frontend"
        self.start_ts = int(time.time())


settings = Settings()
