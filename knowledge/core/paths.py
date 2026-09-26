"""项目路径配置。"""

from pathlib import Path
import os


def get_knowledge_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def get_front_page_dir() -> Path:
    """获取前端页面目录。"""

    return get_knowledge_dir() / "front"


def get_local_base_dir() -> Path:
    """获取导入文件本地保存根目录。"""

    configured_dir = os.getenv("IMPORT_UPLOAD_DIR", "").strip()
    if configured_dir:
        base_dir = Path(configured_dir)
    else:
        base_dir = get_knowledge_dir() / "processor" / "import_process" / "import_temp_Dir"

    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def get_lifecycle_db_path() -> Path:
    """Return the durable stage-3 control-plane database path."""

    configured = os.getenv("LIFECYCLE_DB_PATH", "").strip()
    path = Path(configured) if configured else get_knowledge_dir() / "data" / "lifecycle.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
