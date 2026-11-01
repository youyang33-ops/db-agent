"""项目路径和 .env 配置。"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

# SHOP_DB 环境变量可以覆盖默认数据库路径（测试和攻击演示会用临时数据库）
DEFAULT_DB = Path(os.environ.get("SHOP_DB", PROJECT_ROOT / "data" / "shop.db"))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", PROJECT_ROOT / "reports"))
LOG_DIR = Path(os.environ.get("LOG_DIR", PROJECT_ROOT / "logs"))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
