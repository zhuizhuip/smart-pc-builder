"""应用配置集中管理."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载：读取 KEY=VALUE（支持 # 注释与引号），不覆盖已存在的环境变量。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


class Config:
    """基础配置."""
    SECRET_KEY = os.environ.get("SECRET_KEY", "school-build-secret-key-change-in-production")
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "school-jwt-secret-change-in-production")
    JWT_ACCESS_TOKEN_EXPIRES = 86400  # 24 小时

    # MySQL (Docker)
    MYSQL_HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
    MYSQL_USER = os.environ.get("MYSQL_USER", "school")
    MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "school123")
    MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "school_market")

    # DeepSeek API（从 .env 或系统环境变量读取，代码中不保存明文密钥）
    DEEPSEEK_API_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
    DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
    DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

    # Docker
    COMPOSE_FILE = BASE_DIR / "docker-compose.yml"

    # Paths
    TEMPLATE_FOLDER = str(BASE_DIR / "frontend" / "templates")
    STATIC_FOLDER = str(BASE_DIR / "frontend" / "static")


class DevelopmentConfig(Config):
    """开发环境配置."""
    DEBUG = True


class ProductionConfig(Config):
    """生产环境配置."""
    DEBUG = False


config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
}
