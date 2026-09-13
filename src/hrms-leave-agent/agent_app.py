"""HRMS 請假 Agent 的 Host／MCP Client。

目前包含（任務 #5-5）：
- MCP 工具清單 → OpenAI tools schema：移除 employee_id、隱藏 preview_leave
- LLM tool_call 參數解析，並無條件以啟動身分覆蓋 employee_id
- 工具風險分級（不在表內一律 HIGH）
- 依 README「兩層錯誤契約」解析工具結果

HITL 攔截（#5-6）與對話迴圈（#5-7）之後補上。openai／dotenv 在對話迴圈內才 import，
上面這些純函式測試時不需要 API key。
"""

import copy
import json

from mcp import types

# 只給 Client 內部 hook 呼叫、不交給 LLM 的工具
HIDDEN_TOOLS = frozenset({"preview_leave"})
# 由 Client 注入、LLM 不能決定的參數
INJECTED_ARGUMENT = "employee_id"

TOOL_RISK_TABLE = {
    "query_leave_balance": "LOW",
    "apply_leave": "HIGH",
}

MAX_ERROR_TEXT = 500


class ClientToolError(Exception):
    """Client 端產生的錯誤，轉成與 Server 相同的 {"ok": false, ...} 形狀交給 LLM。"""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message

    def to_result(self) -> dict:
        return {"ok": False, "error_code": self.error_code, "message": self.message}


def risk_level(tool_name: str) -> str:
    """不在風險表內的工具一律視為 HIGH（同 lesson5-4 預設 CRITICAL）。"""
    return TOOL_RISK_TABLE.get(tool_name, "HIGH")


def llm_parameters(input_schema: dict) -> dict:
    """深複製 MCP inputSchema，並把注入參數同時從 properties 與 required 移除。"""
    schema = copy.deepcopy(input_schema)
    schema.pop("title", None)
    schema.get("properties", {}).pop(INJECTED_ARGUMENT, None)
    required = [name for name in schema.get("required", []) if name != INJECTED_ARGUMENT]
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    return schema


def to_openai_tools(mcp_tools: list[types.Tool]) -> list[dict]:
    """MCP list_tools() 結果 → OpenAI function calling 的 tools 參數。"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": llm_parameters(tool.inputSchema),
            },
        }
        for tool in mcp_tools
        if tool.name not in HIDDEN_TOOLS
    ]


def prepare_arguments(raw_arguments: str | None, employee_id: str) -> dict:
    """解析 LLM 給的 arguments JSON，並無條件覆蓋 employee_id。

    LLM 就算自己塞了 employee_id（不在 schema 內）也會被蓋掉。
    """
    if raw_arguments is None or raw_arguments.strip() == "":
        arguments = {}
    else:
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            raise ClientToolError("INVALID_ARGUMENTS", "工具參數不是合法的 JSON") from None
        if not isinstance(arguments, dict):
            raise ClientToolError("INVALID_ARGUMENTS", "工具參數必須是 JSON 物件")
    return {**arguments, INJECTED_ARGUMENT: employee_id}


def parse_tool_result(result: types.CallToolResult) -> dict:
    """依兩層錯誤契約解析：先看 isError，不是才解析 JSON。"""
    text = next(
        (item.text for item in result.content if isinstance(item, types.TextContent)),
        None,
    )

    if result.isError:
        # SDK 參數驗證錯誤，文字不是 JSON；保留內容讓 LLM 有機會修正參數，但限制長度
        message = (text or "工具呼叫失敗")[:MAX_ERROR_TEXT]
        return {"ok": False, "error_code": "TOOL_CALL_ERROR", "message": message}

    if text is None:
        return {"ok": False, "error_code": "INVALID_TOOL_RESULT", "message": "工具沒有回傳文字內容"}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "error_code": "INVALID_TOOL_RESULT", "message": "工具回傳不是合法的 JSON"}
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return {"ok": False, "error_code": "INVALID_TOOL_RESULT", "message": "工具回傳缺少 ok 欄位"}
    return payload
