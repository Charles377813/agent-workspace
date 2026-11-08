"""錯誤信封的 retry 欄位（safe／no／unknown）：Server 依錯誤碼決定，Client 保守補值。"""

import asyncio
import json
from datetime import date

import pytest
from mcp import types

import agent_app
import mcp_server
from agent_app import ClientToolError, build_system_prompt, call_tool_safely, parse_tool_result
from leave_service import LeaveError, retry_disposition


def text_result(text, is_error=False):
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=is_error)


@pytest.mark.parametrize(
    "error_code, expected",
    [
        ("DB_BUSY", "safe"),
        ("INTERNAL_ERROR", "unknown"),
        ("EMPLOYEE_NOT_FOUND", "no"),
        ("LEAVE_TYPE_NOT_FOUND", "no"),
        ("INVALID_TIME_FORMAT", "no"),
        ("INVALID_TIME_RANGE", "no"),
        ("CROSS_YEAR_NOT_SUPPORTED", "no"),
        ("OVERLAPPING_REQUEST", "no"),
        ("INSUFFICIENT_BALANCE", "no"),
    ],
)
def test_retry_disposition_by_error_code(error_code, expected):
    assert retry_disposition(error_code) == expected
    assert LeaveError(error_code, "x").retry == expected


def test_unlisted_error_code_defaults_to_no():
    assert retry_disposition("SOMETHING_NEW") == "no"


def test_server_json_carries_retry_for_business_error():
    def busy():
        raise LeaveError("DB_BUSY", "忙碌")

    payload = json.loads(mcp_server._respond(busy))
    assert payload == {"ok": False, "error_code": "DB_BUSY", "message": "忙碌", "retry": "safe"}


def test_server_json_success_has_no_retry():
    payload = json.loads(mcp_server._respond(lambda: {"hours": 4}))
    assert payload == {"ok": True, "hours": 4}


def test_client_tool_error_is_not_retryable():
    assert ClientToolError("UNKNOWN_TOOL", "x").to_result()["retry"] == "no"


@pytest.mark.parametrize(
    "raw",
    [
        '{"ok": false, "error_code": "X", "message": "m"}',
        '{"ok": false, "error_code": "X", "message": "m", "retry": "maybe"}',
        '{"ok": false, "error_code": "X", "message": "m", "retry": null}',
    ],
)
def test_missing_or_invalid_retry_becomes_unknown(raw):
    assert parse_tool_result(text_result(raw))["retry"] == "unknown"


@pytest.mark.parametrize("value", ["safe", "no", "unknown"])
def test_valid_retry_from_server_is_kept(value):
    raw = json.dumps({"ok": False, "error_code": "X", "message": "m", "retry": value})
    assert parse_tool_result(text_result(raw))["retry"] == value


def test_success_result_is_untouched():
    assert parse_tool_result(text_result('{"ok": true, "hours": 4}')) == {"ok": True, "hours": 4}


@pytest.mark.parametrize("text", ["not json", '{"hours": 4}', '[1, 2]'])
def test_broken_result_is_unknown_because_effect_cannot_be_confirmed(text):
    result = parse_tool_result(text_result(text))
    assert result["error_code"] == "INVALID_TOOL_RESULT"
    assert result["retry"] == "unknown"


def test_result_without_text_is_unknown():
    result = parse_tool_result(types.CallToolResult(content=[], isError=False))
    assert result["error_code"] == "INVALID_TOOL_RESULT"
    assert result["retry"] == "unknown"


def test_sdk_validation_error_is_no():
    assert parse_tool_result(text_result("1 validation error", is_error=True))["retry"] == "no"


def test_channel_exception_is_unknown_not_safe():
    class BrokenSession:
        async def call_tool(self, name, arguments):
            raise ConnectionError("pipe closed")

    result = asyncio.run(call_tool_safely(BrokenSession(), "apply_leave", {}))
    assert result["error_code"] == "TOOL_CALL_ERROR"
    assert result["retry"] == "unknown"


def test_system_prompt_explains_retry_rule():
    prompt = build_system_prompt(date(2026, 9, 16), "E001")
    for expected in ("retry", "safe", "unknown", "不要自動重試"):
        assert expected in prompt
    assert agent_app.RETRY_VALUES == {"safe", "no", "unknown"}
