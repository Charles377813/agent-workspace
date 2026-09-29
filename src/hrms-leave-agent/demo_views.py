"""Gradio demo 的唯讀畫面資料（任務 #6 里程碑 3）：請假週曆、額度快照。

- 純函式（build_*）不碰 Gradio、不碰 DB，方便單元測試。
- 查詢函式（query_*）只開唯讀連線，只讀 leave_requests／leave_balances。
- 顯示值一律是純文字；DB 裡的未知代碼經 display_value 跳脫後原樣顯示，不插入 HTML。

設計見 docs/tasks/06-hrms-gradio-demo.md §7。
"""

import logging
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from agent_app import LEAVE_TYPE_NAMES, WEEKDAY_NAMES, display_value

logger = logging.getLogger(__name__)

# 與服務層時數規則一致：上午 09–13、下午 14–18，只有平日
SLOTS = (("上午", time(9), time(13)), ("下午", time(14), time(18)))
WORKDAYS_PER_WEEK = 5
WEEKS = (("本週", 0), ("下週", 7))
TIME_FORMAT = "%Y-%m-%dT%H:%M"
LEAVE_TYPE_ORDER = ("ANNUAL", "PERSONAL", "SICK")


@dataclass(frozen=True)
class Table:
    title: str
    headers: list[str]
    rows: list[list[str]]


def week_monday(today: date) -> date:
    return today - timedelta(days=today.weekday())


def calendar_range(today: date) -> tuple[datetime, datetime]:
    """[本週一 00:00, 下下週一 00:00)。"""
    monday = week_monday(today)
    start = datetime.combine(monday, time(0))
    return start, start + timedelta(days=14)


def leave_type_label(code: str) -> str:
    return LEAVE_TYPE_NAMES.get(code) or display_value(code)


def _parse(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def build_leave_calendar(requests: Iterable[tuple[str, str, str]], today: date) -> list[Table]:
    """(leave_type, start_at, end_at) → 本週、下週兩張表；列為上午／下午，欄為週一～五。

    假單 [start, end) 與時段 [slot_start, slot_end) 有交集才標記（半開區間，相鄰不標）；
    部分時數標整格、跨時段／跨天標所有交集格；同格多筆以「、」合併。
    """
    parsed = []
    for leave_type, start_at, end_at in requests:
        start, end = _parse(start_at), _parse(end_at)
        if start is None or end is None or start >= end:
            logger.warning("略過格式不正確的假單時間：%r ～ %r", start_at, end_at)
            continue
        parsed.append((leave_type_label(leave_type), start, end))

    monday = week_monday(today)
    tables = []
    for title, offset in WEEKS:
        days = [monday + timedelta(days=offset + i) for i in range(WORKDAYS_PER_WEEK)]
        # 標題盡量短，兩週的表格才不用橫向捲動
        headers = ["時段"] + [f"{day.month}/{day.day} {WEEKDAY_NAMES[day.weekday()]}" for day in days]
        rows = []
        for slot_name, slot_start, slot_end in SLOTS:
            row = [slot_name]
            for day in days:
                cell_start = datetime.combine(day, slot_start)
                cell_end = datetime.combine(day, slot_end)
                labels = []
                for label, start, end in parsed:
                    if start < cell_end and end > cell_start and label not in labels:
                        labels.append(label)
                row.append("、".join(labels))
            rows.append(row)
        tables.append(Table(title, headers, rows))
    return tables


def build_balance_table(balances: Iterable[tuple[str, int, int]]) -> Table:
    """(leave_type, total_hours, used_hours) → 額度快照，依特休、事假、病假排序。"""
    by_type = {leave_type: (total, used) for leave_type, total, used in balances}
    ordered = [code for code in LEAVE_TYPE_ORDER if code in by_type]
    ordered += sorted(code for code in by_type if code not in LEAVE_TYPE_ORDER)
    rows = [
        [leave_type_label(code), str(by_type[code][0]), str(by_type[code][1]), str(by_type[code][0] - by_type[code][1])]
        for code in ordered
    ]
    return Table("額度（小時）", ["假別", "總時數", "已用", "剩餘"], rows)


# ---------------------------------------------------------------------------
# 唯讀查詢
# ---------------------------------------------------------------------------


def _connect_read_only(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def query_calendar_requests(db_path: str | Path, employee_id: str, today: date) -> list[tuple[str, str, str]]:
    start, end = calendar_range(today)
    with closing(_connect_read_only(db_path)) as conn:
        return conn.execute(
            """
            SELECT leave_type, start_at, end_at FROM leave_requests
            WHERE employee_id = ? AND status = 'SUBMITTED'
              AND start_at < ? AND end_at > ?
            ORDER BY start_at
            """,
            (employee_id, end.strftime(TIME_FORMAT), start.strftime(TIME_FORMAT)),
        ).fetchall()


def query_balances(db_path: str | Path, employee_id: str, year: int) -> list[tuple[str, int, int]]:
    with closing(_connect_read_only(db_path)) as conn:
        return conn.execute(
            "SELECT leave_type, total_hours, used_hours FROM leave_balances WHERE employee_id = ? AND year = ?",
            (employee_id, year),
        ).fetchall()


def load_views(db_path: str | Path, employee_id: str, today: date) -> tuple[list[Table], Table]:
    """週曆＋額度；DB 讀取失敗時回空表並記 log，不讓畫面刷新失敗。"""
    try:
        calendar = build_leave_calendar(query_calendar_requests(db_path, employee_id, today), today)
        balance = build_balance_table(query_balances(db_path, employee_id, today.year))
    except sqlite3.Error:
        logger.exception("讀取週曆／額度失敗")
        return build_leave_calendar([], today), build_balance_table([])
    return calendar, balance
