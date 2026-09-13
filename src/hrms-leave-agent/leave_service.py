"""請假業務邏輯：時間規則、時數計算、驗證、餘額、重疊、寫入交易。

不依賴 MCP 與 LLM，可直接單元測試。規則說明見 README「時間與時數規則」「資料庫存取與交易」。
業務錯誤一律以 LeaveError 拋出，由 mcp_server 轉成 JSON。
"""

import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "leave.db"
BUSY_TIMEOUT_SECONDS = 5.0

TIME_FORMAT = "%Y-%m-%dT%H:%M"
# 用 [0-9] 而非 \d：\d 會匹配全形等 Unicode 數字，strptime 也會接受
_TIME_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:00")

# 工作時段，半開區間 [start, end)
WORK_BLOCKS = ((time(9), time(13)), (time(14), time(18)))
VALID_START_HOURS = frozenset({9, 10, 11, 12, 14, 15, 16, 17})
VALID_END_HOURS = frozenset({10, 11, 12, 13, 15, 16, 17, 18})


class LeaveError(Exception):
    """業務錯誤，error_code 對應 README 的錯誤碼表。"""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def parse_time(value: str) -> datetime:
    """嚴格解析 YYYY-MM-DDTHH:00；秒數、時區、純日期、非整點一律拒絕。"""
    if not isinstance(value, str) or not _TIME_PATTERN.fullmatch(value):
        raise LeaveError("INVALID_TIME_FORMAT", f"時間格式須為 YYYY-MM-DDTHH:00：{value!r}")
    try:
        # 正則只管形狀，不存在的日期（2026-02-30）與 24 點靠 strptime 擋
        return datetime.strptime(value, TIME_FORMAT)
    except ValueError:
        raise LeaveError("INVALID_TIME_FORMAT", f"不存在的日期或時間：{value!r}") from None


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def compute_hours(start_at: str, end_at: str) -> int:
    """回傳請假區間 [start_at, end_at) 內的工作時數。

    起訖必須是平日的合法邊界，不裁切；只計平日的 09–13、14–18。
    """
    start = parse_time(start_at)
    end = parse_time(end_at)

    if start.year != end.year:
        raise LeaveError("CROSS_YEAR_NOT_SUPPORTED", "不支援跨年度請假，請分成兩張假單")
    if end <= start:
        raise LeaveError("INVALID_TIME_RANGE", "結束時間必須晚於開始時間")
    if not _is_weekday(start.date()) or start.hour not in VALID_START_HOURS:
        raise LeaveError("INVALID_TIME_RANGE", f"開始時間不是平日的合法時段起點：{start_at}")
    if not _is_weekday(end.date()) or end.hour not in VALID_END_HOURS:
        raise LeaveError("INVALID_TIME_RANGE", f"結束時間不是平日的合法時段終點：{end_at}")

    total = timedelta()
    # 以天數位移迭代，不在最後一天之後再加一天（避免 9999-12-31 溢位）
    for offset in range((end.date() - start.date()).days + 1):
        day = start.date() + timedelta(days=offset)
        if not _is_weekday(day):
            continue
        for block_start, block_end in WORK_BLOCKS:
            overlap = min(end, datetime.combine(day, block_end)) - max(
                start, datetime.combine(day, block_start)
            )
            if overlap > timedelta():
                total += overlap

    hours = int(total.total_seconds() // 3600)
    if hours <= 0:
        raise LeaveError("INVALID_TIME_RANGE", "請假區間內沒有任何工作時數")
    return hours


# ---------------------------------------------------------------------------
# 資料庫存取
# ---------------------------------------------------------------------------


def connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """短生命週期連線：關閉 Python 隱式交易、啟用外鍵。"""
    path = db_path or os.environ.get("HRMS_DB_PATH") or DEFAULT_DB_PATH
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _rollback(conn: sqlite3.Connection | None) -> None:
    if conn is not None and conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            # 不讓清理失敗蓋掉原本的錯誤；close() 仍會丟棄未提交的交易
            pass


def _close(conn: sqlite3.Connection | None) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except sqlite3.Error:
        # 結果已由 COMMIT 或 ROLLBACK 決定，關閉失敗不應蓋掉回傳值或原本的錯誤
        pass


def _run(db_path, work, *, write: bool):
    """開連線 → （寫入時 BEGIN IMMEDIATE）→ work(conn) → COMMIT；任何失敗回滾並轉成 LeaveError。"""
    conn = None
    try:
        conn = connect(db_path)
        if write:
            conn.execute("BEGIN IMMEDIATE")
        result = work(conn)
        if write:
            conn.execute("COMMIT")
        return result
    except LeaveError:
        _rollback(conn)
        raise
    except sqlite3.OperationalError as exc:
        _rollback(conn)
        # sqlite_errorcode 可能是延伸碼，取低 8 位還原成主錯誤碼
        if (exc.sqlite_errorcode & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise LeaveError("DB_BUSY", "資料庫忙碌中，請稍後再試") from None
        raise LeaveError("INTERNAL_ERROR", "系統錯誤，請稍後再試") from exc
    except Exception as exc:
        _rollback(conn)
        # 不把原始例外內容放進 message，避免外洩給 LLM
        raise LeaveError("INTERNAL_ERROR", "系統錯誤，請稍後再試") from exc
    finally:
        _close(conn)


def _require_employee(conn: sqlite3.Connection, employee_id: str) -> None:
    if not isinstance(employee_id, str) or conn.execute(
        "SELECT 1 FROM employees WHERE id = ?", (employee_id,)
    ).fetchone() is None:
        raise LeaveError("EMPLOYEE_NOT_FOUND", f"找不到員工：{employee_id!r}")


def _require_leave_type(conn: sqlite3.Connection, leave_type: str) -> None:
    if not isinstance(leave_type, str) or conn.execute(
        "SELECT 1 FROM leave_types WHERE code = ?", (leave_type,)
    ).fetchone() is None:
        raise LeaveError("LEAVE_TYPE_NOT_FOUND", f"找不到假別：{leave_type!r}")


def _remaining_hours(conn: sqlite3.Connection, employee_id: str, leave_type: str, year: int) -> int | None:
    row = conn.execute(
        "SELECT total_hours - used_hours AS remaining FROM leave_balances"
        " WHERE employee_id = ? AND leave_type = ? AND year = ?",
        (employee_id, leave_type, year),
    ).fetchone()
    return None if row is None else row["remaining"]


def _evaluate(conn, employee_id, leave_type, start_at, end_at) -> dict:
    """preview 與 apply 共用的驗證與試算，順序：員工 → 假別 → 時間 → 重疊 → 餘額。"""
    _require_employee(conn, employee_id)
    _require_leave_type(conn, leave_type)
    hours = compute_hours(start_at, end_at)
    year = parse_time(start_at).year

    # 半開區間、不分假別；時間字串固定格式，字典序即時間序
    overlapping = conn.execute(
        "SELECT id FROM leave_requests"
        " WHERE employee_id = ? AND status = 'SUBMITTED' AND start_at < ? AND end_at > ?"
        " LIMIT 1",
        (employee_id, end_at, start_at),
    ).fetchone()
    if overlapping is not None:
        raise LeaveError("OVERLAPPING_REQUEST", f"與既有假單 #{overlapping['id']} 時間重疊")

    remaining = _remaining_hours(conn, employee_id, leave_type, year)
    # 沒有額度資料視同餘額不足（MVP）
    if remaining is None or remaining < hours:
        raise LeaveError(
            "INSUFFICIENT_BALANCE",
            f"{year} 年剩餘 {remaining or 0} 小時，不足本次 {hours} 小時",
        )
    return {"hours": hours, "year": year, "remaining_before": remaining}


# ---------------------------------------------------------------------------
# 對外操作（mcp_server 呼叫）
# ---------------------------------------------------------------------------


def query_leave_balance(
    employee_id: str,
    leave_type: str | None = None,
    year: int | None = None,
    *,
    db_path=None,
    today: date | None = None,
) -> list[dict]:
    """查剩餘假數；leave_type 省略＝全部假別，year 省略＝今年。"""
    year = year if year is not None else (today or date.today()).year

    def work(conn):
        _require_employee(conn, employee_id)
        sql = (
            "SELECT leave_type, total_hours, used_hours, total_hours - used_hours AS remaining_hours"
            " FROM leave_balances WHERE employee_id = ? AND year = ?"
        )
        params: list = [employee_id, year]
        if leave_type is not None:
            _require_leave_type(conn, leave_type)
            sql += " AND leave_type = ?"
            params.append(leave_type)
        rows = conn.execute(sql + " ORDER BY leave_type", params).fetchall()
        return [dict(row) for row in rows]

    return _run(db_path, work, write=False)


def preview_leave(
    employee_id: str,
    leave_type: str,
    start_at: str,
    end_at: str,
    reason: str | None = None,
    *,
    db_path=None,
) -> dict:
    """唯讀試算：回傳時數與扣抵前後餘額，不寫入任何資料。"""

    def work(conn):
        result = _evaluate(conn, employee_id, leave_type, start_at, end_at)
        return {
            "hours": result["hours"],
            "remaining_before": result["remaining_before"],
            "remaining_after": result["remaining_before"] - result["hours"],
        }

    return _run(db_path, work, write=False)


def apply_leave(
    employee_id: str,
    leave_type: str,
    start_at: str,
    end_at: str,
    reason: str | None = None,
    *,
    db_path=None,
) -> dict:
    """在單一 BEGIN IMMEDIATE 交易內重新驗證、扣抵、寫假單、寫稽核。"""

    def work(conn):
        result = _evaluate(conn, employee_id, leave_type, start_at, end_at)
        hours, year = result["hours"], result["year"]

        updated = conn.execute(
            "UPDATE leave_balances SET used_hours = used_hours + ?"
            " WHERE employee_id = ? AND leave_type = ? AND year = ?"
            " AND total_hours - used_hours >= ?",
            (hours, employee_id, leave_type, year, hours),
        ).rowcount
        if updated == 0:
            # 已在寫入鎖內驗證過，理論上不會發生；保留條件式扣抵作為最後防線
            raise LeaveError("INSUFFICIENT_BALANCE", f"{year} 年剩餘假數不足本次 {hours} 小時")

        request_id = conn.execute(
            "INSERT INTO leave_requests (employee_id, leave_type, start_at, end_at, hours, reason)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (employee_id, leave_type, start_at, end_at, hours, reason),
        ).lastrowid
        conn.execute(
            "INSERT INTO audit_logs (action_type, detail) VALUES (?, ?)",
            (
                "APPLY_LEAVE",
                f"假單 #{request_id}：員工 {employee_id} {leave_type} {start_at}～{end_at} 共 {hours} 小時",
            ),
        )
        return {
            "request_id": request_id,
            "hours": hours,
            "remaining_hours": result["remaining_before"] - hours,
        }

    return _run(db_path, work, write=True)
