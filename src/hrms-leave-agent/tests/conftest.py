import sqlite3
from pathlib import Path

import pytest

INIT_SQL = Path(__file__).resolve().parents[1] / "init_db.sql"


@pytest.fixture
def db_path(tmp_path):
    """每個測試一個全新的暫存 DB（跑 init_db.sql 種子資料）。"""
    path = tmp_path / "leave.db"
    conn = sqlite3.connect(path)
    conn.executescript(INIT_SQL.read_text(encoding="utf-8"))
    conn.close()
    return path


@pytest.fixture
def db(db_path):
    """測試用的直接查詢連線，用來斷言資料狀態或佈置情境。"""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()
