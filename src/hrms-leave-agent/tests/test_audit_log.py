"""稽核紀錄（R5）：成功與失敗都記、欄位結構化、應用層不能 UPDATE／DELETE、重置流程正常。"""

import sqlite3

import pytest

from conftest import INIT_SQL
from leave_service import LeaveError, apply_leave

WED_PM = ("2026-09-16T14:00", "2026-09-16T18:00")  # 4 小時


def audit_rows(db):
    return [dict(row) for row in db.execute("SELECT * FROM audit_logs ORDER BY id")]


def test_success_is_recorded_with_structured_fields(db_path, db):
    result = apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)
    (row,) = audit_rows(db)
    assert row["action_type"] == "APPLY_LEAVE"
    assert row["actor"] == "E001"
    assert row["outcome"] == "SUCCESS"
    assert row["error_code"] is None
    assert row["leave_request_id"] == result["request_id"]
    assert row["timestamp"]


@pytest.mark.parametrize(
    "args, error_code",
    [
        (("E002", "ANNUAL", *WED_PM), "INSUFFICIENT_BALANCE"),  # E002 特休已用完
        (("E999", "ANNUAL", *WED_PM), "EMPLOYEE_NOT_FOUND"),
        (("E001", "NOPE", *WED_PM), "LEAVE_TYPE_NOT_FOUND"),
        (("E001", "ANNUAL", "2026-09-16T09:30", "2026-09-16T12:00"), "INVALID_TIME_FORMAT"),
    ],
)
def test_failure_is_recorded_and_changes_nothing_else(db_path, db, args, error_code):
    with pytest.raises(LeaveError) as exc:
        apply_leave(*args, db_path=db_path)
    assert exc.value.error_code == error_code
    (row,) = audit_rows(db)
    assert row["outcome"] == "FAILURE"
    assert row["error_code"] == error_code
    assert row["actor"] == args[0]
    assert row["leave_request_id"] is None
    assert db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] == 0


def test_failure_record_survives_even_though_business_transaction_rolled_back(db_path, db):
    """失敗紀錄寫在回滾之後的獨立交易，不會跟著業務交易一起消失。"""
    with pytest.raises(LeaveError):
        apply_leave("E002", "ANNUAL", *WED_PM, db_path=db_path)
    assert len(audit_rows(db)) == 1


def test_forged_inputs_are_sanitized_in_audit(db_path, db):
    evil = "E001\n[2026-01-01] APPLY_LEAVE SUCCESS " + "x" * 500
    with pytest.raises(LeaveError):
        apply_leave(evil, "ANNUAL", *WED_PM, db_path=db_path)
    with pytest.raises(LeaveError):
        apply_leave(None, "ANNUAL", *WED_PM, db_path=db_path)
    rows = audit_rows(db)
    assert len(rows) == 2
    for row in rows:
        assert "\n" not in row["actor"] and len(row["actor"]) <= 64
        assert "\n" not in row["detail"] and len(row["detail"]) <= 300


def test_audit_write_failure_does_not_change_the_error_returned(db_path, db):
    db.execute("DROP TRIGGER audit_logs_no_update")  # 確認 trigger 之外的失敗路徑：讓 INSERT 本身失敗
    db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_logs BEGIN SELECT RAISE(ABORT, 'x'); END")
    with pytest.raises(LeaveError) as exc:
        apply_leave("E002", "ANNUAL", *WED_PM, db_path=db_path)
    assert exc.value.error_code == "INSUFFICIENT_BALANCE"


def test_update_is_blocked(db_path, db):
    apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE audit_logs SET detail = 'tampered'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE audit_logs SET outcome = 'FAILURE', error_code = 'X'")


def test_delete_is_blocked(db_path, db):
    apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM audit_logs")
    assert len(audit_rows(db)) == 1


def test_outcome_and_error_code_must_be_consistent(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO audit_logs (action_type, actor, outcome, detail) VALUES ('A', 'E001', 'FAILURE', 'd')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO audit_logs (action_type, actor, outcome, detail) VALUES ('A', 'E001', 'MAYBE', 'd')")


def test_reset_script_clears_audit_and_restores_triggers(db_path, db):
    apply_leave("E001", "ANNUAL", *WED_PM, db_path=db_path)
    assert len(audit_rows(db)) == 1
    db.executescript(INIT_SQL.read_text(encoding="utf-8"))  # 重置：有 trigger 保護的表也要能重建
    assert audit_rows(db) == []
    db.execute("INSERT INTO audit_logs (action_type, actor, outcome, detail) VALUES ('A', 'E001', 'SUCCESS', 'd')")
    assert len(audit_rows(db)) == 1
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM audit_logs")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE audit_logs SET detail = 'x'")
    assert len(audit_rows(db)) == 1


def test_reset_script_upgrades_old_schema_db(tmp_path):
    """舊 schema 的 leave.db（只有 action_type／detail）重跑 init_db.sql 即升級。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, action_type TEXT NOT NULL, detail TEXT NOT NULL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO audit_logs (action_type, detail) VALUES ('APPLY_LEAVE', 'old')")
    conn.commit()
    conn.executescript(INIT_SQL.read_text(encoding="utf-8"))
    columns = [r[1] for r in conn.execute("PRAGMA table_info(audit_logs)")]
    conn.close()
    assert {"actor", "outcome", "error_code", "leave_request_id"} <= set(columns)
