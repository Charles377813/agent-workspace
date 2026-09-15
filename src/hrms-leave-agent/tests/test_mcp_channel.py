"""MCP 通道測試：真的用 stdio 啟動 mcp_server.py 子行程，走握手 → list_tools → call_tool。

不連 LLM。對應 README 測試策略第 2 層。2026-09-16 是週三。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_DIR = Path(__file__).resolve().parents[1]
WED_PM = {"start_at": "2026-09-16T14:00", "end_at": "2026-09-16T18:00"}


def server_params(db_path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_DIR / "mcp_server.py")],
        cwd=str(PROJECT_DIR),
        # stdio_client 預設只傳少數安全環境變數，DB 路徑要明確帶進去
        env={**os.environ, "HRMS_DB_PATH": str(db_path)},
    )


def tool_json(result) -> dict:
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


async def run_session(db_path):
    async with stdio_client(server_params(db_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools

            preview = tool_json(
                await session.call_tool("preview_leave", {"employee_id": "E001", "leave_type": "ANNUAL", **WED_PM})
            )
            applied = tool_json(
                await session.call_tool("apply_leave", {"employee_id": "E001", "leave_type": "ANNUAL", **WED_PM})
            )
            overlap = tool_json(
                await session.call_tool("apply_leave", {"employee_id": "E001", "leave_type": "SICK", **WED_PM})
            )
            balance = tool_json(
                await session.call_tool("query_leave_balance", {"employee_id": "E001", "leave_type": "ANNUAL", "year": 2026})
            )
            invalid_enum = await session.call_tool(
                "preview_leave", {"employee_id": "E001", "leave_type": "VACATION", **WED_PM}
            )
            return tools, preview, applied, overlap, balance, invalid_enum


def test_stdio_channel_end_to_end(db_path, db):
    tools, preview, applied, overlap, balance, invalid_enum = asyncio.run(run_session(db_path))

    assert {tool.name for tool in tools} == {"query_leave_balance", "preview_leave", "apply_leave"}
    assert all("employee_id" in tool.inputSchema["required"] for tool in tools)

    assert preview == {"ok": True, "hours": 4, "remaining_before": 56, "remaining_after": 52}
    assert applied == {"ok": True, "request_id": 1, "hours": 4, "remaining_hours": 52}
    assert overlap["ok"] is False and overlap["error_code"] == "OVERLAPPING_REQUEST"
    assert balance["balances"][0]["remaining_hours"] == 52

    # schema 以外的假別由 SDK 參數驗證擋下，不會進到服務層
    assert invalid_enum.isError

    # 子行程確實寫進同一個暫存 DB
    assert db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 1
