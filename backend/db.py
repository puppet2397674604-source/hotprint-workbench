"""Read-only SQLite access and schema diagnostics."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Dict, Iterator, List, Optional
from urllib.parse import quote

REQUIRED_TABLES = ("category_sales_batches", "category_sales_observations")
REQUIRED_VIEWS = ("category_sales_by_category",)


@dataclass(frozen=True)
class DatabaseStatus:
    state: str  # ready | missing | empty_schema | empty_data | error
    path: str
    exists: bool
    read_only: bool
    batch_count: int
    observation_count: int
    missing_objects: List[str]
    message: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "state": self.state,
            "path": self.path,
            "exists": self.exists,
            "read_only": self.read_only,
            "batch_count": self.batch_count,
            "observation_count": self.observation_count,
            "missing_objects": list(self.missing_objects),
            "message": self.message,
        }


def readonly_uri(db_path: Path) -> str:
    resolved = db_path.expanduser().resolve()
    return f"file:{quote(str(resolved))}?mode=ro"


@contextmanager
def open_readonly(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open SQLite strictly in read-only URI mode so queries can never mutate data."""
    conn = sqlite3.connect(readonly_uri(db_path), uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def inspect_database(db_path: Path) -> DatabaseStatus:
    resolved = db_path.expanduser().resolve()
    if not resolved.exists():
        return DatabaseStatus(
            state="missing",
            path=str(resolved),
            exists=False,
            read_only=True,
            batch_count=0,
            observation_count=0,
            missing_objects=list(REQUIRED_TABLES + REQUIRED_VIEWS),
            message="未找到 SQLite 数据库文件。请确认 sorftime-example/category_sales.sqlite3 存在，或通过 --db 参数指定有效快照路径。",
        )

    try:
        with open_readonly(resolved) as conn:
            existing = {
                (row["type"], row["name"])
                for row in conn.execute(
                    "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            missing: List[str] = []
            for table in REQUIRED_TABLES:
                if ("table", table) not in existing:
                    missing.append(table)
            for view in REQUIRED_VIEWS:
                if ("view", view) not in existing:
                    missing.append(view)

            if missing:
                return DatabaseStatus(
                    state="empty_schema",
                    path=str(resolved),
                    exists=True,
                    read_only=True,
                    batch_count=0,
                    observation_count=0,
                    missing_objects=missing,
                    message=f"数据库缺少必要表或视图：{', '.join(missing)}。",
                )

            batch_count = int(
                conn.execute("SELECT COUNT(*) FROM category_sales_batches").fetchone()[0]
            )
            obs_count = int(
                conn.execute("SELECT COUNT(*) FROM category_sales_observations").fetchone()[0]
            )
            if batch_count == 0 or obs_count == 0:
                return DatabaseStatus(
                    state="empty_data",
                    path=str(resolved),
                    exists=True,
                    read_only=True,
                    batch_count=batch_count,
                    observation_count=obs_count,
                    missing_objects=[],
                    message="数据库结构完整，但当前尚未包含任何采集批次或商品观察记录。",
                )

            return DatabaseStatus(
                state="ready",
                path=str(resolved),
                exists=True,
                read_only=True,
                batch_count=batch_count,
                observation_count=obs_count,
                missing_objects=[],
                message=f"只读连接就绪：{batch_count} 个批次，{obs_count} 条类目观察记录。",
            )
    except sqlite3.Error as exc:
        return DatabaseStatus(
            state="error",
            path=str(resolved),
            exists=True,
            read_only=True,
            batch_count=0,
            observation_count=0,
            missing_objects=[],
            message=f"无法读取 SQLite 数据库：{exc}",
        )


def file_signature(db_path: Path) -> Optional[tuple]:
    try:
        stat = db_path.stat()
        return (str(db_path.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
