"""HRMS 請假 MCP Server（MCP Python SDK v1 / FastMCP，stdio）。

只做薄包裝：參數轉給 leave_service，結果轉成 JSON 字串。
業務邏輯與錯誤碼見 leave_service.py 與 README「工具規格」。

employee_id 在這裡是必填參數；「不給 LLM 看、由 Client 覆蓋」是 agent_app.py 的責任。
"""

import json
import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP

import leave_service
from leave_service import LeaveError

LeaveType = Literal["ANNUAL", "PERSONAL", "SICK"]

mcp = FastMCP("HRMS Leave MCP Server")
logger = logging.getLogger(__name__)


def _error_json(error_code: str, message: str) -> str:
    return json.dumps({"ok": False, "error_code": error_code, "message": message}, ensure_ascii=False)


def _respond(fn, *args, **kwargs) -> str:
    """成功 {"ok": true, ...}；業務錯誤 {"ok": false, "error_code", "message"}。

    非預期例外（含 JSON 序列化失敗）也轉成 INTERNAL_ERROR，不讓 FastMCP 把原始例外文字回給 Client。
    完整 traceback 只寫進 Server 的 log（stderr；stdout 被 JSON-RPC 佔用）。
    """
    try:
        return json.dumps({"ok": True, **fn(*args, **kwargs)}, ensure_ascii=False)
    except LeaveError as exc:
        return _error_json(exc.error_code, exc.message)
    except Exception:
        logger.exception("tool 執行發生非預期例外")
        return _error_json("INTERNAL_ERROR", "系統錯誤，請稍後再試")


@mcp.tool()
def query_leave_balance(
    employee_id: str,
    leave_type: LeaveType | None = None,
    year: int | None = None,
) -> str:
    """查詢員工的剩餘假數（單位：小時，1 天 = 8 小時）。

    假別：ANNUAL＝特休、PERSONAL＝事假、SICK＝病假；省略 leave_type 會列出全部假別。
    省略 year 代表今年。
    """
    return _respond(
        lambda: {"balances": leave_service.query_leave_balance(employee_id, leave_type, year)}
    )


@mcp.tool()
def preview_leave(
    employee_id: str,
    leave_type: LeaveType,
    start_at: str,
    end_at: str,
    reason: str | None = None,
) -> str:
    """【唯讀試算】計算請假時數與扣抵前後餘額，不會送出假單。

    時間格式 YYYY-MM-DDTHH:00（台北時間、整點）。工作時段：上午 09:00–13:00、下午 14:00–18:00，只計平日。
    """
    return _respond(
        leave_service.preview_leave, employee_id, leave_type, start_at, end_at, reason
    )


@mcp.tool()
def apply_leave(
    employee_id: str,
    leave_type: LeaveType,
    start_at: str,
    end_at: str,
    reason: str | None = None,
) -> str:
    """【高風險・寫入】送出請假申請並扣除假數。

    假別：ANNUAL＝特休、PERSONAL＝事假、SICK＝病假。
    時間格式 YYYY-MM-DDTHH:00（台北時間、整點）。「上午」＝09:00–13:00、「下午」＝14:00–18:00、「整天」＝09:00–18:00，只計平日。
    使用者沒說清楚假別或日期時，先反問，不要猜。
    """
    return _respond(
        leave_service.apply_leave, employee_id, leave_type, start_at, end_at, reason
    )


if __name__ == "__main__":
    mcp.run()
