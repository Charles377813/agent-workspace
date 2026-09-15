"""MCP 工具的薄包裝層：JSON 格式、錯誤轉換、schema。不啟動子行程。2026-09-16 是週三。"""

import asyncio
import json

import pytest

import mcp_server

WED_PM = ("2026-09-16T14:00", "2026-09-16T18:00")


@pytest.fixture(autouse=True)
def use_temp_db(db_path, monkeypatch):
    monkeypatch.setenv("HRMS_DB_PATH", str(db_path))


def call(tool, *args, **kwargs):
    return json.loads(tool(*args, **kwargs))


def test_query_leave_balance_wraps_rows():
    result = call(mcp_server.query_leave_balance, "E001", "ANNUAL", 2026)
    assert result == {
        "ok": True,
        "balances": [{"leave_type": "ANNUAL", "total_hours": 80, "used_hours": 24, "remaining_hours": 56}],
    }


def test_preview_leave_success():
    assert call(mcp_server.preview_leave, "E001", "ANNUAL", *WED_PM) == {
        "ok": True,
        "hours": 4,
        "remaining_before": 56,
        "remaining_after": 52,
    }


def test_apply_leave_success(db):
    result = call(mcp_server.apply_leave, "E001", "ANNUAL", *WED_PM, "看牙醫")
    assert result == {"ok": True, "request_id": 1, "hours": 4, "remaining_hours": 52}
    assert db.execute("SELECT reason FROM leave_requests").fetchone()[0] == "看牙醫"


@pytest.mark.parametrize(
    "tool, args, error_code",
    [
        (mcp_server.query_leave_balance, ("E999",), "EMPLOYEE_NOT_FOUND"),
        (mcp_server.preview_leave, ("E002", "ANNUAL", *WED_PM), "INSUFFICIENT_BALANCE"),
        (mcp_server.apply_leave, ("E001", "ANNUAL", "2026-09-16T14:30", "2026-09-16T18:00"), "INVALID_TIME_FORMAT"),
    ],
)
def test_business_errors_become_json(tool, args, error_code):
    result = call(tool, *args)
    assert result["ok"] is False
    assert result["error_code"] == error_code
    assert result["message"]


def test_unexpected_exception_becomes_internal_error_without_leaking(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(mcp_server.leave_service, "preview_leave", broken)
    raw = mcp_server.preview_leave("E001", "ANNUAL", *WED_PM)
    assert json.loads(raw) == {"ok": False, "error_code": "INTERNAL_ERROR", "message": "系統錯誤，請稍後再試"}
    assert "secret" not in raw
    # 細節留在 Server log，方便除錯
    assert "secret internal detail" in caplog.text


def test_unserializable_payload_becomes_internal_error(monkeypatch):
    monkeypatch.setattr(mcp_server.leave_service, "preview_leave", lambda *args, **kwargs: {"hours": object()})
    result = call(mcp_server.preview_leave, "E001", "ANNUAL", *WED_PM)
    assert result["ok"] is False
    assert result["error_code"] == "INTERNAL_ERROR"


def test_json_keeps_chinese_readable():
    raw = mcp_server.apply_leave("E999", "ANNUAL", *WED_PM)
    assert "找不到員工" in raw


def test_tool_schemas():
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert set(tools) == {"query_leave_balance", "preview_leave", "apply_leave"}

    for tool in tools.values():
        schema = tool.inputSchema
        assert "employee_id" in schema["properties"]
        assert "employee_id" in schema["required"]
        assert tool.description

    for name in ("preview_leave", "apply_leave"):
        schema = tools[name].inputSchema
        assert set(schema["required"]) == {"employee_id", "leave_type", "start_at", "end_at"}
        assert schema["properties"]["leave_type"]["enum"] == ["ANNUAL", "PERSONAL", "SICK"]

    query_schema = tools["query_leave_balance"].inputSchema
    assert query_schema["required"] == ["employee_id"]
    # 選填參數以 anyOf[..., null] 表示，enum 約束仍要在
    leave_type_options = query_schema["properties"]["leave_type"]["anyOf"]
    assert {"enum": ["ANNUAL", "PERSONAL", "SICK"], "type": "string"} in leave_type_options
    assert {"type": "null"} in leave_type_options
    assert {"type": "integer"} in query_schema["properties"]["year"]["anyOf"]
    assert "高風險" in tools["apply_leave"].description
