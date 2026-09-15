"""服務層：查詢、試算、寫入交易。種子資料見 init_db.sql；2026-09-16 是週三。"""

import sqlite3
import threading
from datetime import date

import pytest

import leave_service
from leave_service import LeaveError, apply_leave, preview_leave, query_leave_balance

WED_PM = ("2026-09-16T14:00", "2026-09-16T18:00")  # 4 小時
WED_AM = ("2026-09-16T09:00", "2026-09-16T13:00")  # 4 小時


def snapshot(db):
    """三張會被 apply 動到的表的完整內容。"""
    return {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
        for table in ("leave_balances", "leave_requests", "audit_logs")
    }


def remaining(db, employee_id, leave_type, year=2026):
    return db.execute(
        "SELECT total_hours - used_hours FROM leave_balances"
        " WHERE employee_id = ? AND leave_type = ? AND year = ?",
        (employee_id, leave_type, year),
    ).fetchone()[0]


def expect_error(error_code, fn, *args, **kwargs):
    with pytest.raises(LeaveError) as exc:
        fn(*args, **kwargs)
    assert exc.value.error_code == error_code
    return exc.value


# ---------------------------------------------------------------------------
# 連線
# ---------------------------------------------------------------------------


def test_connect_enables_foreign_keys_and_disables_implicit_transactions(db_path):
    conn = leave_service.connect(db_path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.isolation_level is None
    finally:
        conn.close()


def test_connect_uses_env_db_path(db_path, monkeypatch):
    monkeypatch.setenv("HRMS_DB_PATH", str(db_path))
    assert query_leave_balance("E001", "ANNUAL", 2026)[0]["remaining_hours"] == 56


# ---------------------------------------------------------------------------
# query_leave_balance
# ---------------------------------------------------------------------------


def test_query_all_types(db_path):
    rows = query_leave_balance("E001", year=2026, db_path=db_path)
    assert rows == [
        {"leave_type": "ANNUAL", "total_hours": 80, "used_hours": 24, "remaining_hours": 56},
        {"leave_type": "PERSONAL", "total_hours": 112, "used_hours": 0, "remaining_hours": 112},
        {"leave_type": "SICK", "total_hours": 240, "used_hours": 8, "remaining_hours": 232},
    ]


def test_query_single_type_and_default_year_from_today(db_path):
    rows = query_leave_balance("E002", "ANNUAL", db_path=db_path, today=date(2026, 9, 13))
    assert rows == [{"leave_type": "ANNUAL", "total_hours": 56, "used_hours": 56, "remaining_hours": 0}]


def test_query_year_without_data_returns_empty(db_path):
    assert query_leave_balance("E001", year=2027, db_path=db_path) == []


def test_query_unknown_employee(db_path):
    expect_error("EMPLOYEE_NOT_FOUND", query_leave_balance, "E999", db_path=db_path)


def test_query_unknown_leave_type(db_path):
    expect_error("LEAVE_TYPE_NOT_FOUND", query_leave_balance, "E001", "VACATION", db_path=db_path)


# ---------------------------------------------------------------------------
# preview_leave
# ---------------------------------------------------------------------------


def test_preview_success_does_not_write(db_path, db):
    before = snapshot(db)
    result = preview_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)
    assert result == {"hours": 4, "remaining_before": 56, "remaining_after": 52}
    assert snapshot(db) == before


@pytest.mark.parametrize(
    "args, error_code",
    [
        (("E999", "ANNUAL", *WED_PM), "EMPLOYEE_NOT_FOUND"),
        ((None, "ANNUAL", *WED_PM), "EMPLOYEE_NOT_FOUND"),
        (("E001", "VACATION", *WED_PM), "LEAVE_TYPE_NOT_FOUND"),
        (("E001", "ANNUAL", "2026-09-16T14:30", "2026-09-16T18:00"), "INVALID_TIME_FORMAT"),
        (("E001", "ANNUAL", "2026-09-16T13:00", "2026-09-16T18:00"), "INVALID_TIME_RANGE"),
        (("E001", "ANNUAL", "2026-12-31T09:00", "2027-01-04T10:00"), "CROSS_YEAR_NOT_SUPPORTED"),
        (("E002", "ANNUAL", *WED_PM), "INSUFFICIENT_BALANCE"),  # 特休已用完
        (("E001", "ANNUAL", "2027-01-04T09:00", "2027-01-04T13:00"), "INSUFFICIENT_BALANCE"),  # 無 2027 額度
    ],
)
def test_preview_errors(db_path, db, args, error_code):
    before = snapshot(db)
    expect_error(error_code, preview_leave, *args, db_path=db_path)
    assert snapshot(db) == before


def test_validation_order_employee_before_leave_type_before_time(db_path):
    expect_error("EMPLOYEE_NOT_FOUND", preview_leave, "E999", "VACATION", "bad", "bad", db_path=db_path)
    expect_error("LEAVE_TYPE_NOT_FOUND", preview_leave, "E001", "VACATION", "bad", "bad", db_path=db_path)


# ---------------------------------------------------------------------------
# apply_leave：成功路徑
# ---------------------------------------------------------------------------


def test_apply_success_updates_three_tables(db_path, db):
    result = apply_leave("E001", "ANNUAL", *WED_PM, reason="看牙醫", db_path=db_path)
    assert result == {"request_id": 1, "hours": 4, "remaining_hours": 52}

    assert remaining(db, "E001", "ANNUAL") == 52
    request = db.execute("SELECT * FROM leave_requests").fetchall()
    assert len(request) == 1
    assert dict(request[0]) | {"created_at": None} == {
        "id": 1,
        "employee_id": "E001",
        "leave_type": "ANNUAL",
        "start_at": WED_PM[0],
        "end_at": WED_PM[1],
        "hours": 4,
        "reason": "看牙醫",
        "status": "SUBMITTED",
        "created_at": None,
    }
    audits = db.execute("SELECT action_type, detail FROM audit_logs").fetchall()
    assert [row["action_type"] for row in audits] == ["APPLY_LEAVE"]
    assert "假單 #1" in audits[0]["detail"] and "E001" in audits[0]["detail"]


def test_apply_preview_and_apply_agree(db_path):
    preview = preview_leave("E001", "SICK", *WED_AM, db_path=db_path)
    applied = apply_leave("E001", "SICK", *WED_AM, db_path=db_path)
    assert applied["hours"] == preview["hours"]
    assert applied["remaining_hours"] == preview["remaining_after"]


def test_apply_exactly_remaining_balance_succeeds(db_path, db):
    db.execute("UPDATE leave_balances SET total_hours = 4, used_hours = 0 WHERE employee_id = 'E001' AND leave_type = 'PERSONAL'")
    result = apply_leave("E001", "PERSONAL", *WED_PM, db_path=db_path)
    assert result["remaining_hours"] == 0
    assert remaining(db, "E001", "PERSONAL") == 0


def test_sequential_applies_cannot_overspend(db_path, db):
    db.execute("UPDATE leave_balances SET total_hours = 4, used_hours = 0 WHERE employee_id = 'E001' AND leave_type = 'PERSONAL'")
    apply_leave("E001", "PERSONAL", *WED_AM, db_path=db_path)
    before = snapshot(db)
    expect_error("INSUFFICIENT_BALANCE", apply_leave, "E001", "PERSONAL", *WED_PM, db_path=db_path)
    assert snapshot(db) == before
    assert remaining(db, "E001", "PERSONAL") == 0


# ---------------------------------------------------------------------------
# apply_leave：失敗不寫入
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, error_code",
    [
        (("E999", "ANNUAL", *WED_PM), "EMPLOYEE_NOT_FOUND"),
        (("E001", "VACATION", *WED_PM), "LEAVE_TYPE_NOT_FOUND"),
        (("E001", "ANNUAL", "2026-09-16", "2026-09-17"), "INVALID_TIME_FORMAT"),
        (("E001", "ANNUAL", "2026-09-19T09:00", "2026-09-19T18:00"), "INVALID_TIME_RANGE"),
        (("E002", "ANNUAL", *WED_PM), "INSUFFICIENT_BALANCE"),
        (("E001", "ANNUAL", "2027-01-04T09:00", "2027-01-04T13:00"), "INSUFFICIENT_BALANCE"),
    ],
)
def test_apply_errors_leave_data_unchanged(db_path, db, args, error_code):
    before = snapshot(db)
    expect_error(error_code, apply_leave, *args, db_path=db_path)
    assert snapshot(db) == before


# ---------------------------------------------------------------------------
# 重疊
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "second",
    [
        ("2026-09-16T14:00", "2026-09-16T18:00"),  # 完全相同
        ("2026-09-16T15:00", "2026-09-16T16:00"),  # 包在裡面
        ("2026-09-16T12:00", "2026-09-16T15:00"),  # 前段交疊
        ("2026-09-16T17:00", "2026-09-17T10:00"),  # 後段交疊、跨日
        ("2026-09-16T09:00", "2026-09-17T18:00"),  # 整個包住
    ],
)
def test_overlap_rejected_across_leave_types(db_path, db, second):
    apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)
    before = snapshot(db)
    error = expect_error("OVERLAPPING_REQUEST", apply_leave, "E001", "SICK", *second, db_path=db_path)
    assert "#1" in error.message
    assert snapshot(db) == before
    expect_error("OVERLAPPING_REQUEST", preview_leave, "E001", "SICK", *second, db_path=db_path)


@pytest.mark.parametrize(
    "first, second",
    [
        (("2026-09-16T14:00", "2026-09-16T16:00"), ("2026-09-16T16:00", "2026-09-16T18:00")),
        (("2026-09-16T16:00", "2026-09-16T18:00"), ("2026-09-16T14:00", "2026-09-16T16:00")),
        (WED_AM, WED_PM),
    ],
)
def test_adjacent_requests_are_not_overlapping(db_path, first, second):
    apply_leave("E001", "ANNUAL", *first, db_path=db_path)
    assert apply_leave("E001", "ANNUAL", *second, db_path=db_path)["request_id"] == 2


def test_other_employee_does_not_overlap(db_path):
    apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)
    assert apply_leave("E003", "ANNUAL", *WED_PM, db_path=db_path)["request_id"] == 2


def test_non_submitted_request_does_not_overlap(db_path, db):
    apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)
    db.execute("UPDATE leave_requests SET status = 'CANCELLED' WHERE id = 1")
    assert apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)["request_id"] == 2


# ---------------------------------------------------------------------------
# 回滾與錯誤轉換
# ---------------------------------------------------------------------------


def test_audit_insert_failure_rolls_back_everything(db_path, db):
    db.execute(
        "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_logs"
        " BEGIN SELECT RAISE(ABORT, 'secret internal detail'); END"
    )
    before = snapshot(db)
    error = expect_error("INTERNAL_ERROR", apply_leave, "E001", "ANNUAL", *WED_PM, db_path=db_path)
    assert "secret" not in error.message
    assert snapshot(db) == before
    assert remaining(db, "E001", "ANNUAL") == 56


def test_request_insert_failure_rolls_back_balance(db_path, db):
    db.execute(
        "CREATE TRIGGER fail_request BEFORE INSERT ON leave_requests"
        " BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    before = snapshot(db)
    expect_error("INTERNAL_ERROR", apply_leave, "E001", "ANNUAL", *WED_PM, db_path=db_path)
    assert snapshot(db) == before


def test_no_lock_left_after_failed_apply(db_path, db):
    """失敗的 apply 結束後 DB 沒有殘留的鎖或未結束交易，下一次寫入正常。"""
    db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_logs BEGIN SELECT RAISE(ABORT, 'x'); END")
    expect_error("INTERNAL_ERROR", apply_leave, "E001", "ANNUAL", *WED_PM, db_path=db_path)
    db.execute("DROP TRIGGER fail_audit")
    assert apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)["remaining_hours"] == 52


def test_write_lock_held_by_other_connection_returns_db_busy(db_path, db, monkeypatch):
    monkeypatch.setattr(leave_service, "BUSY_TIMEOUT_SECONDS", 0.1)
    before = snapshot(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        expect_error("DB_BUSY", apply_leave, "E001", "ANNUAL", *WED_PM, db_path=db_path)
    finally:
        db.execute("ROLLBACK")
    assert snapshot(db) == before


def test_unexpected_exception_becomes_internal_error(db_path, db, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("unexpected secret")

    monkeypatch.setattr(leave_service, "compute_hours", broken)
    before = snapshot(db)
    error = expect_error("INTERNAL_ERROR", apply_leave, "E001", "ANNUAL", *WED_PM, db_path=db_path)
    assert "secret" not in error.message
    assert snapshot(db) == before


class CleanupFailingConnection:
    """包住真實連線，讓 ROLLBACK 或 close() 故意失敗，模擬清理步驟出錯。"""

    def __init__(self, conn, *, fail_rollback=False, fail_close=False):
        self._conn = conn
        self._fail_rollback = fail_rollback
        self._fail_close = fail_close

    @property
    def in_transaction(self):
        return self._conn.in_transaction

    def execute(self, sql, *args):
        if self._fail_rollback and sql == "ROLLBACK":
            raise sqlite3.OperationalError("rollback failed: secret")
        return self._conn.execute(sql, *args)

    def close(self):
        self._conn.close()
        if self._fail_close:
            raise sqlite3.OperationalError("close failed: secret")


def patch_connect(monkeypatch, **failures):
    real_connect = leave_service.connect
    monkeypatch.setattr(
        leave_service,
        "connect",
        lambda db_path=None: CleanupFailingConnection(real_connect(db_path), **failures),
    )


def test_rollback_failure_keeps_original_business_error(db_path, db, monkeypatch):
    patch_connect(monkeypatch, fail_rollback=True)
    before = snapshot(db)
    expect_error("INSUFFICIENT_BALANCE", apply_leave, "E002", "ANNUAL", *WED_PM, db_path=db_path)
    assert snapshot(db) == before


def test_rollback_failure_after_unexpected_error_is_still_internal_error(db_path, db, monkeypatch):
    db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_logs BEGIN SELECT RAISE(ABORT, 'x'); END")
    patch_connect(monkeypatch, fail_rollback=True)
    before = snapshot(db)
    error = expect_error("INTERNAL_ERROR", apply_leave, "E001", "ANNUAL", *WED_PM, db_path=db_path)
    assert "secret" not in error.message
    # ROLLBACK 失敗時，close() 仍會丟棄未提交的交易
    assert snapshot(db) == before


def test_close_failure_does_not_hide_committed_result(db_path, db, monkeypatch):
    patch_connect(monkeypatch, fail_close=True)
    assert apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)["remaining_hours"] == 52
    assert remaining(db, "E001", "ANNUAL") == 52


def test_close_failure_does_not_hide_business_error(db_path, monkeypatch):
    patch_connect(monkeypatch, fail_close=True)
    expect_error("EMPLOYEE_NOT_FOUND", apply_leave, "E999", "ANNUAL", *WED_PM, db_path=db_path)


def test_concurrent_same_slot_second_request_validates_after_first_commits(db_path, db, monkeypatch):
    """第一筆停在寫入鎖內時，第二筆必須等它提交後才驗證，因此看得到重疊。

    若驗證被移到 BEGIN IMMEDIATE 之前，第二筆會在第一筆暫停期間通過重疊檢查，
    事件順序與結果都會不同。
    """
    first_inside_lock = threading.Event()
    release_first = threading.Event()
    events = []
    events_lock = threading.Lock()
    real_compute_hours = leave_service.compute_hours

    def record(event):
        with events_lock:
            events.append(event)

    def paused_compute_hours(start_at, end_at):
        name = threading.current_thread().name
        record(f"{name}:validate")
        if name == "first":
            first_inside_lock.set()
            assert release_first.wait(timeout=5)
        return real_compute_hours(start_at, end_at)

    monkeypatch.setattr(leave_service, "compute_hours", paused_compute_hours)
    outcomes = {}

    def worker():
        name = threading.current_thread().name
        try:
            outcomes[name] = apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)
        except LeaveError as exc:
            outcomes[name] = exc.error_code
        record(f"{name}:done")

    first = threading.Thread(target=worker, name="first")
    second = threading.Thread(target=worker, name="second")
    first.start()
    assert first_inside_lock.wait(timeout=5)
    second.start()
    # 讓第二筆有時間撞上寫入鎖；若它沒被擋住，這段時間內就會記下 second:validate
    second.join(timeout=0.3)
    assert second.is_alive()
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert events == ["first:validate", "first:done", "second:validate", "second:done"]
    assert outcomes["first"]["request_id"] == 1
    assert outcomes["second"] == "OVERLAPPING_REQUEST"
    assert db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] == 1
    assert remaining(db, "E001", "ANNUAL") == 52


def test_concurrent_applies_do_not_overspend(db_path, db):
    """兩條執行緒同時請最後 4 小時，只能成功一個。"""
    db.execute("UPDATE leave_balances SET total_hours = 4, used_hours = 0 WHERE employee_id = 'E001' AND leave_type = 'PERSONAL'")
    requests = [WED_AM, WED_PM]
    results, errors = [], []
    barrier = threading.Barrier(len(requests))

    def worker(times):
        barrier.wait()
        try:
            results.append(apply_leave("E001", "PERSONAL", *times, db_path=db_path))
        except LeaveError as exc:
            errors.append(exc.error_code)

    threads = [threading.Thread(target=worker, args=(times,)) for times in requests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1
    assert errors == ["INSUFFICIENT_BALANCE"]
    assert remaining(db, "E001", "PERSONAL") == 0
    assert db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] == 1
