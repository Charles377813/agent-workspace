"""Client 端 schema 轉換、employee_id 覆蓋、風險分級、工具結果解析。不連 LLM。2026-09-16 是週三。"""

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import agent_app
import mcp_server
from agent_app import ClientToolError, parse_tool_result, prepare_arguments, risk_level, to_openai_tools

PROJECT_DIR = Path(__file__).resolve().parents[1]


def text_result(text, is_error=False):
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=is_error)


# ---------------------------------------------------------------------------
# schema 轉換
# ---------------------------------------------------------------------------


def real_mcp_tools():
    # FastMCP 每次 list_tools() 回傳的 inputSchema 是內部註冊表的同一個 dict；
    # 深拷貝避免任何測試（或有 bug 的實作）污染後續測試
    return [tool.model_copy(deep=True) for tool in asyncio.run(mcp_server.mcp.list_tools())]


def test_openai_tools_from_real_server_schema():
    tools = to_openai_tools(real_mcp_tools())
    by_name = {tool["function"]["name"]: tool for tool in tools}

    assert set(by_name) == {"query_leave_balance", "apply_leave"}  # preview_leave 被隱藏
    assert all(tool["type"] == "function" for tool in tools)

    for tool in tools:
        parameters = tool["function"]["parameters"]
        assert "employee_id" not in parameters["properties"]
        assert "employee_id" not in parameters.get("required", [])
        assert "title" not in parameters
        assert tool["function"]["description"]

    apply_parameters = by_name["apply_leave"]["function"]["parameters"]
    assert set(apply_parameters["required"]) == {"leave_type", "start_at", "end_at"}
    assert apply_parameters["properties"]["leave_type"]["enum"] == ["ANNUAL", "PERSONAL", "SICK"]

    # query 移除 employee_id 後沒有必填參數，不留空的 required
    assert "required" not in by_name["query_leave_balance"]["function"]["parameters"]


def test_conversion_does_not_mutate_mcp_schema():
    input_schema = {
        "title": "apply_leaveArguments",
        "type": "object",
        "properties": {"employee_id": {"type": "string"}, "leave_type": {"type": "string"}},
        "required": ["employee_id", "leave_type"],
    }
    tool = types.Tool(name="apply_leave", description="x", inputSchema=input_schema)
    before = copy.deepcopy(tool.inputSchema)
    # 前提：來源確實含有要被移除的欄位，否則這個測試驗不到任何東西
    assert "employee_id" in before["properties"] and "employee_id" in before["required"]

    to_openai_tools([tool])

    assert tool.inputSchema == before


def test_conversion_does_not_mutate_server_registry():
    to_openai_tools(asyncio.run(mcp_server.mcp.list_tools()))
    fresh = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert "employee_id" in fresh["apply_leave"].inputSchema["properties"]
    assert "employee_id" in fresh["apply_leave"].inputSchema["required"]


@pytest.mark.parametrize(
    "input_schema, expected",
    [
        (
            {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
            {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        ),
        (
            {"type": "object", "properties": {"employee_id": {"type": "string"}}},
            {"type": "object", "properties": {}},
        ),
        ({"type": "object"}, {"type": "object"}),
    ],
)
def test_conversion_handles_schema_shapes(input_schema, expected):
    tool = types.Tool(name="other_tool", description=None, inputSchema=input_schema)
    [converted] = to_openai_tools([tool])
    assert converted["function"]["parameters"] == expected
    assert converted["function"]["description"] == ""


# ---------------------------------------------------------------------------
# 參數覆蓋
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"leave_type": "ANNUAL"}', {"leave_type": "ANNUAL", "employee_id": "E001"}),
        ('{"leave_type": "ANNUAL", "employee_id": "E003"}', {"leave_type": "ANNUAL", "employee_id": "E001"}),
        ('{"employee_id": null}', {"employee_id": "E001"}),
        ("{}", {"employee_id": "E001"}),
        ("", {"employee_id": "E001"}),
        (None, {"employee_id": "E001"}),
    ],
)
def test_prepare_arguments_always_overrides_employee_id(raw, expected):
    assert prepare_arguments(raw, "E001") == expected


@pytest.mark.parametrize("raw", ["not json", "[]", "null", '"E003"', "123", '{"a": 1'])
def test_prepare_arguments_rejects_non_object_json(raw):
    with pytest.raises(ClientToolError) as exc:
        prepare_arguments(raw, "E001")
    assert exc.value.error_code == "INVALID_ARGUMENTS"
    assert exc.value.to_result()["ok"] is False


# ---------------------------------------------------------------------------
# 風險分級
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name, level",
    [
        ("query_leave_balance", "LOW"),
        ("apply_leave", "HIGH"),
        ("preview_leave", "HIGH"),  # 不給 LLM；若 LLM 硬叫也不能自動放行
        ("delete_everything", "HIGH"),
        ("", "HIGH"),
    ],
)
def test_risk_level_defaults_to_high(tool_name, level):
    assert risk_level(tool_name) == level


# ---------------------------------------------------------------------------
# 工具結果解析（兩層錯誤契約）
# ---------------------------------------------------------------------------


def test_parse_success_json():
    assert parse_tool_result(text_result('{"ok": true, "hours": 4}')) == {"ok": True, "hours": 4}


def test_parse_business_error_json_passes_through():
    raw = '{"ok": false, "error_code": "INSUFFICIENT_BALANCE", "message": "不足"}'
    assert parse_tool_result(text_result(raw)) == json.loads(raw)


def test_parse_sdk_error_is_not_json_decoded():
    result = parse_tool_result(text_result("Error executing tool preview_leave: 1 validation error", is_error=True))
    assert result == {
        "ok": False,
        "error_code": "TOOL_CALL_ERROR",
        "message": "Error executing tool preview_leave: 1 validation error",
    }


def test_parse_sdk_error_that_looks_like_success_json_is_still_error():
    result = parse_tool_result(text_result('{"ok": true}', is_error=True))
    assert result["ok"] is False and result["error_code"] == "TOOL_CALL_ERROR"


def test_parse_sdk_error_text_is_truncated():
    result = parse_tool_result(text_result("x" * 5000, is_error=True))
    assert len(result["message"]) == agent_app.MAX_ERROR_TEXT


def test_parse_sdk_error_without_text():
    result = parse_tool_result(types.CallToolResult(content=[], isError=True))
    assert result["error_code"] == "TOOL_CALL_ERROR" and result["message"]


@pytest.mark.parametrize(
    "result",
    [
        text_result("not json"),
        text_result("[1, 2]"),
        text_result('{"hours": 4}'),  # 缺 ok
        text_result('{"ok": "yes"}'),  # ok 不是布林
        types.CallToolResult(content=[], isError=False),
        types.CallToolResult(
            content=[types.ImageContent(type="image", data="AAAA", mimeType="image/png")], isError=False
        ),
    ],
)
def test_parse_invalid_results(result):
    parsed = parse_tool_result(result)
    assert parsed["ok"] is False
    assert parsed["error_code"] == "INVALID_TOOL_RESULT"


# ---------------------------------------------------------------------------
# 接真的 stdio Server：覆蓋與錯誤契約端到端
# ---------------------------------------------------------------------------


async def run_client_flow(db_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_DIR / "mcp_server.py")],
        cwd=str(PROJECT_DIR),
        env={**os.environ, "HRMS_DB_PATH": str(db_path)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            openai_tools = to_openai_tools((await session.list_tools()).tools)

            # LLM 想查 E002 的特休（E002 已用完），Client 以啟動身分 E001 覆蓋
            spoofed = prepare_arguments('{"employee_id": "E002", "leave_type": "ANNUAL", "year": 2026}', "E001")
            balance = parse_tool_result(await session.call_tool("query_leave_balance", spoofed))

            # 假別不在 enum → SDK 層錯誤（isError）
            invalid_enum = parse_tool_result(
                await session.call_tool(
                    "apply_leave",
                    prepare_arguments(
                        '{"leave_type": "VACATION", "start_at": "2026-09-16T14:00", "end_at": "2026-09-16T18:00"}',
                        "E001",
                    ),
                )
            )

            # 業務錯誤（ok:false）
            insufficient = parse_tool_result(
                await session.call_tool(
                    "apply_leave",
                    prepare_arguments(
                        '{"leave_type": "ANNUAL", "start_at": "2026-09-16T14:00", "end_at": "2026-09-16T18:00"}',
                        "E002",
                    ),
                )
            )
            return openai_tools, balance, invalid_enum, insufficient


def test_client_helpers_against_real_stdio_server(db_path, db):
    openai_tools, balance, invalid_enum, insufficient = asyncio.run(run_client_flow(db_path))

    assert {tool["function"]["name"] for tool in openai_tools} == {"query_leave_balance", "apply_leave"}

    assert balance["ok"] is True
    assert balance["balances"] == [
        {"leave_type": "ANNUAL", "total_hours": 80, "used_hours": 24, "remaining_hours": 56}
    ]  # E001 的資料，不是 E002

    assert invalid_enum["ok"] is False and invalid_enum["error_code"] == "TOOL_CALL_ERROR"
    assert insufficient["ok"] is False and insufficient["error_code"] == "INSUFFICIENT_BALANCE"

    assert db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] == 0
