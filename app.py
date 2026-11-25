#!/usr/bin/env python3
"""Local entry point for Printpool HotPrint Workbench V0.1 (default http://127.0.0.1:8772)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

WORKBENCH_DIR = Path(__file__).resolve().parent
if str(WORKBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(WORKBENCH_DIR))

from backend.collector import bootstrap_live_database
from backend.config import DEFAULT_DB_PATH, DEFAULT_HOST, DEFAULT_LIVE_DB_PATH, DEFAULT_PORT
from backend.preprocessing import DEFAULT_APP_DB_PATH
from backend.server import create_server


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Printpool 印花热点智能选品工作台本地服务"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite 数据库路径（安全模式默认只读挂载原始快照；开启真实采集默认自动克隆并写入工作台独立库 data/category_sales_live.sqlite3，不改原库）",
    )
    parser.add_argument(
        "--app-db",
        type=Path,
        default=DEFAULT_APP_DB_PATH,
        help=f"预处理与人工审核持久化 SQLite 路径（默认 {DEFAULT_APP_DB_PATH}）",
    )
    parser.add_argument(
        "--allow-live-collection",
        action="store_true",
        help="显式允许经前端两步确认后执行真实 Sorftime 付费采集（写入工作台独立库，不改原始快照备份）",
    )
    args = parser.parse_args()

    if args.db is not None:
        effective_db = args.db
    elif args.allow_live_collection:
        effective_db = bootstrap_live_database(DEFAULT_DB_PATH, DEFAULT_LIVE_DB_PATH)
    else:
        effective_db = DEFAULT_DB_PATH

    server, app = create_server(
        host=args.host,
        port=args.port,
        db_path=effective_db,
        app_db_path=args.app_db,
        allow_live_collection=args.allow_live_collection,
    )
    status = app.repo.status()
    print(f"HotPrint Workbench running at http://{args.host}:{args.port}", flush=True)
    print(
        f"Database [{status.state}]: {status.path} (writable_live={args.allow_live_collection})",
        flush=True,
    )
    print(f"Live paid collection enabled: {args.allow_live_collection}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down HotPrint Workbench...", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
