"""HRMS 請假 Agent 的 Host／MCP Client。

目前包含：
- （#5-5）MCP 工具清單 → OpenAI tools schema：移除 employee_id、隱藏 preview_leave
- （#5-5）LLM tool_call 參數解析，並無條件以啟動身分覆蓋 employee_id
- （#5-5）工具風險分級（不在表內一律 HIGH）
- （#5-5）依 README「兩層錯誤契約」解析工具結果
- （#5-6）dispatch_tool_call：工具名 allowlist → 覆蓋身分 → LOW 直接呼叫／HIGH 走 HITL
  （凍結參數 → Client 自行試算 → y/N → 以同一份參數送出）

對話迴圈（#5-7）之後補上。openai／dotenv 在對話迴圈內才 import，測試時不需要 API key。
"""

import copy
import json
import logging
import unicodedata
from collections.abc import Callable, Collection

from mcp import types

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# HITL 與工具分派（#5-6）
# ---------------------------------------------------------------------------

# HIGH 風險工具 → 確認前由 Client 自行呼叫的唯讀試算工具
PREVIEW_TOOLS = {"apply_leave": "preview_leave"}
LEAVE_TYPE_NAMES = {"ANNUAL": "特休", "PERSONAL": "事假", "SICK": "病假"}
CONFIRM_ANSWERS = frozenset({"y", "yes"})

Confirm = Callable[[str], bool]


def terminal_confirm(prompt: str) -> bool:
    """預設確認方式：終端機 y/N。只有明確輸入 y／yes 才算同意；EOF（例如 stdin 被關閉）視為拒絕。"""
    try:
        answer = input(f"{prompt}\n👉 確認送出？(y/N): ")
    except EOFError:
        return False
    return answer.strip().lower() in CONFIRM_ANSWERS


MAX_DISPLAY_CHARS = 200
# Cc：C0/C1 控制字元（含 \n \r \t ESC）；Cf：格式字元（含 bidi 覆寫、零寬字元）；Zl/Zp：Unicode 分行／分段
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def display_value(value) -> str:
    """把要顯示在終端機的動態值轉成單行、可見的安全字串。

    控制字元一律轉成 \\n、\\x1b、\\u202e 這類可見跳脫，避免 LLM 用換行或 ANSI 序列偽造確認畫面。
    只影響顯示；實際送出的參數不做任何修改。
    """
    text = str(value)
    escaped = "".join(
        ch.encode("unicode_escape").decode("ascii") if unicodedata.category(ch) in _UNSAFE_CATEGORIES else ch
        for ch in text
    )
    if len(escaped) > MAX_DISPLAY_CHARS:
        escaped = escaped[:MAX_DISPLAY_CHARS] + f"…（共 {len(escaped)} 字，已截斷顯示）"
    return escaped


def format_confirmation(arguments: dict, preview: dict) -> str:
    """確認畫面：身分與時間取自凍結參數，時數與餘額取自以同一份參數試算的結果。

    所有動態值都經過 display_value，畫面固定只有這幾行。
    """
    leave_type = arguments.get("leave_type")
    show = display_value
    lines = [
        "📝 請確認假單內容",
        f"  員工：{show(arguments.get(INJECTED_ARGUMENT))}",
        f"  假別：{show(LEAVE_TYPE_NAMES.get(leave_type, leave_type))}（{show(leave_type)}）",
        f"  時間：{show(arguments.get('start_at'))} ～ {show(arguments.get('end_at'))}",
        f"  時數：{show(preview.get('hours'))} 小時",
        f"  餘額：{show(preview.get('remaining_before'))} → {show(preview.get('remaining_after'))} 小時",
    ]
    if arguments.get("reason"):
        lines.append(f"  事由：{show(arguments['reason'])}")
    return "\n".join(lines)


async def call_tool_safely(session, name: str, arguments: dict) -> dict:
    """呼叫 MCP 工具並解析；通道或 SDK 例外轉成 TOOL_CALL_ERROR，不把例外文字交給 LLM。

    傳入的是參數副本，Server／SDK 端就算改動 dict 也影響不到呼叫端凍結的參數。
    """
    try:
        result = await session.call_tool(name, copy.deepcopy(arguments))
    except Exception:
        logger.exception("呼叫工具 %s 失敗", name)
        return {"ok": False, "error_code": "TOOL_CALL_ERROR", "message": "工具呼叫失敗，請稍後再試"}
    return parse_tool_result(result)


async def dispatch_tool_call(
    session,
    name: str,
    raw_arguments: str | None,
    *,
    employee_id: str,
    allowed_tools: Collection[str],
    confirm: Confirm = terminal_confirm,
) -> dict:
    """處理一個 LLM 發出的 tool_call，回傳要交回給 LLM 的結果（{"ok": ...} dict）。

    allowed_tools 應是實際交給 LLM 的工具名；preview_leave 不在其中，只能由本函式內部呼叫。
    """
    if name not in allowed_tools:
        return ClientToolError("UNKNOWN_TOOL", f"沒有提供名為 {name!r} 的工具").to_result()

    try:
        arguments = prepare_arguments(raw_arguments, employee_id)
    except ClientToolError as exc:
        return exc.to_result()

    if risk_level(name) == "LOW":
        return await call_tool_safely(session, name, arguments)

    # HIGH：凍結參數，之後的試算、確認畫面、送出全部用這一份
    frozen = copy.deepcopy(arguments)

    preview_tool = PREVIEW_TOOLS.get(name)
    if preview_tool is None:
        # 沒有對應試算工具的 HIGH 工具不應出現在 allowlist；fail closed，不詢問也不呼叫
        return ClientToolError("UNKNOWN_TOOL", f"工具 {name!r} 沒有對應的確認流程").to_result()

    preview = await call_tool_safely(session, preview_tool, frozen)
    if not preview.get("ok"):
        # 試算失敗（餘額不足、時間不合法…）不詢問使用者，直接讓 LLM 說明
        return preview

    if not confirm(format_confirmation(frozen, preview)):
        return ClientToolError("USER_REJECTED", "使用者取消送出").to_result()

    return await call_tool_safely(session, name, frozen)
