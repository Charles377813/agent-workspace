"""Client HITL 與工具分派：allowlist、身分覆蓋、試算綁定、y/N。不連 LLM。2026-09-16 是週三。"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import agent_app
from agent_app import dispatch_tool_call, format_confirmation, terminal_confirm

PROJECT_DIR = Path(__file__).resolve().parents[1]
ALLOWED = frozenset({"query_leave_balance", "apply_leave"})
APPLY_ARGS = {"leave_type": "ANNUAL", "start_at": "2026-09-16T14:00", "end_at": "2026-09-16T18:00", "reason": "看牙醫"}
PREVIEW_OK = {"ok": True, "hours": 4, "remaining_before": 56, "remaining_after": 52}


def json_result(payload, is_error=False):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=is_error)


class FakeSession:
    """記錄每次 call_tool 的參數；回應依工具名設定。可模擬 Server 端改動收到的 dict。"""

    def __init__(self, responses=None, *, mutate_received=False, raise_on=None):
        self.calls = []
        self.responses = responses or {}
        self.mutate_received = mutate_received
        self.raise_on = raise_on or set()

    async def call_tool(self, name, arguments):
        self.calls.append((name, json.loads(json.dumps(arguments))))
        if name in self.raise_on:
            raise RuntimeError("connection lost: secret detail")
        if self.mutate_received:
            arguments["start_at"] = "2099-01-01T09:00"
            arguments["employee_id"] = "E999"
        return self.responses.get(name, json_result({"ok": True}))


class RecordingConfirm:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answer


def dispatch(session, name, arguments, confirm, employee_id="E001", allowed=ALLOWED):
    raw = arguments if isinstance(arguments, str) or arguments is None else json.dumps(arguments, ensure_ascii=False)
    return asyncio.run(
        dispatch_tool_call(session, name, raw, employee_id=employee_id, allowed_tools=allowed, confirm=confirm)
    )


# ---------------------------------------------------------------------------
# allowlist 與參數
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["preview_leave", "delete_everything", "", "APPLY_LEAVE"])
def test_tool_not_given_to_llm_is_rejected_without_calling(name):
    session, confirm = FakeSession(), RecordingConfirm(True)
    result = dispatch(session, name, APPLY_ARGS, confirm)
    assert result["ok"] is False and result["error_code"] == "UNKNOWN_TOOL"
    assert session.calls == [] and confirm.prompts == []


@pytest.mark.parametrize("name", ["preview_leave", "internal_low_tool"])
def test_allowlist_blocks_low_risk_tool_not_given_to_llm(monkeypatch, name):
    """風險表把工具標成 LOW（例如唯讀的 preview_leave）也不代表 LLM 可以直接呼叫。

    沒有這條 allowlist 時，LOW 工具會直接被呼叫；fail-closed 的 HIGH 分支擋不到這種情況。
    """
    monkeypatch.setitem(agent_app.TOOL_RISK_TABLE, name, "LOW")
    session, confirm = FakeSession(), RecordingConfirm(True)
    result = dispatch(session, name, APPLY_ARGS, confirm)
    assert result["error_code"] == "UNKNOWN_TOOL"
    assert session.calls == [] and confirm.prompts == []


def test_invalid_arguments_rejected_without_calling():
    session, confirm = FakeSession(), RecordingConfirm(True)
    result = dispatch(session, "apply_leave", "not json", confirm)
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert session.calls == [] and confirm.prompts == []


def test_high_tool_without_preview_mapping_fails_closed(monkeypatch):
    monkeypatch.setitem(agent_app.TOOL_RISK_TABLE, "bulk_apply", "HIGH")
    session, confirm = FakeSession(), RecordingConfirm(True)
    result = dispatch(session, "bulk_apply", {}, confirm, allowed=ALLOWED | {"bulk_apply"})
    assert result["error_code"] == "UNKNOWN_TOOL"
    assert session.calls == [] and confirm.prompts == []


def test_tool_allowed_but_missing_from_risk_table_is_treated_as_high(monkeypatch):
    session, confirm = FakeSession(), RecordingConfirm(True)
    result = dispatch(session, "export_data", {}, confirm, allowed=ALLOWED | {"export_data"})
    assert result["error_code"] == "UNKNOWN_TOOL"  # HIGH 且無試算工具 → 不呼叫
    assert session.calls == []


# ---------------------------------------------------------------------------
# LOW
# ---------------------------------------------------------------------------


def test_low_tool_called_directly_with_overridden_identity():
    session = FakeSession({"query_leave_balance": json_result({"ok": True, "balances": []})})
    confirm = RecordingConfirm(False)
    result = dispatch(session, "query_leave_balance", {"employee_id": "E003", "leave_type": "ANNUAL"}, confirm)
    assert result == {"ok": True, "balances": []}
    assert session.calls == [("query_leave_balance", {"leave_type": "ANNUAL", "employee_id": "E001"})]
    assert confirm.prompts == []


# ---------------------------------------------------------------------------
# HIGH：試算綁定與 y/N
# ---------------------------------------------------------------------------


def test_apply_confirmed_uses_same_frozen_arguments_for_preview_and_apply():
    session = FakeSession(
        {
            "preview_leave": json_result(PREVIEW_OK),
            "apply_leave": json_result({"ok": True, "request_id": 1, "hours": 4, "remaining_hours": 52}),
        }
    )
    confirm = RecordingConfirm(True)
    result = dispatch(session, "apply_leave", {**APPLY_ARGS, "employee_id": "E002"}, confirm)

    assert result == {"ok": True, "request_id": 1, "hours": 4, "remaining_hours": 52}
    expected_args = {**APPLY_ARGS, "employee_id": "E001"}
    assert session.calls == [("preview_leave", expected_args), ("apply_leave", expected_args)]
    assert len(confirm.prompts) == 1


def test_confirmation_shows_frozen_arguments_and_server_preview():
    session = FakeSession({"preview_leave": json_result(PREVIEW_OK)})
    confirm = RecordingConfirm(False)
    dispatch(session, "apply_leave", APPLY_ARGS, confirm)
    [prompt] = confirm.prompts
    for expected in ("E001", "特休", "ANNUAL", "2026-09-16T14:00", "2026-09-16T18:00", "4 小時", "56 → 52", "看牙醫"):
        assert expected in prompt


def test_server_mutating_received_arguments_cannot_change_what_is_submitted():
    session = FakeSession({"preview_leave": json_result(PREVIEW_OK)}, mutate_received=True)
    dispatch(session, "apply_leave", APPLY_ARGS, RecordingConfirm(True))
    expected_args = {**APPLY_ARGS, "employee_id": "E001"}
    assert session.calls == [("preview_leave", expected_args), ("apply_leave", expected_args)]


def test_user_rejects_apply_is_not_called():
    session = FakeSession({"preview_leave": json_result(PREVIEW_OK)})
    result = dispatch(session, "apply_leave", APPLY_ARGS, RecordingConfirm(False))
    assert result == {"ok": False, "error_code": "USER_REJECTED", "message": "使用者取消送出"}
    assert [name for name, _ in session.calls] == ["preview_leave"]


@pytest.mark.parametrize(
    "preview_response, error_code",
    [
        (json_result({"ok": False, "error_code": "INSUFFICIENT_BALANCE", "message": "不足"}), "INSUFFICIENT_BALANCE"),
        (json_result("Error executing tool preview_leave: validation", is_error=True), "TOOL_CALL_ERROR"),
        (json_result("not json"), "INVALID_TOOL_RESULT"),
    ],
)
def test_preview_failure_returns_error_without_asking(preview_response, error_code):
    session = FakeSession({"preview_leave": preview_response})
    confirm = RecordingConfirm(True)
    result = dispatch(session, "apply_leave", APPLY_ARGS, confirm)
    assert result["ok"] is False and result["error_code"] == error_code
    assert confirm.prompts == []
    assert [name for name, _ in session.calls] == ["preview_leave"]


@pytest.mark.parametrize("failing_tool", ["preview_leave", "apply_leave"])
def test_channel_exception_becomes_tool_call_error_without_leaking(failing_tool):
    session = FakeSession({"preview_leave": json_result(PREVIEW_OK)}, raise_on={failing_tool})
    result = dispatch(session, "apply_leave", APPLY_ARGS, RecordingConfirm(True))
    assert result["ok"] is False and result["error_code"] == "TOOL_CALL_ERROR"
    assert "secret" not in result["message"]


def test_apply_business_error_after_confirm_passes_through():
    """試算到送出之間狀態改變（例如另一請求先扣額度），送出時的錯誤原樣交給 LLM。"""
    session = FakeSession(
        {
            "preview_leave": json_result(PREVIEW_OK),
            "apply_leave": json_result({"ok": False, "error_code": "INSUFFICIENT_BALANCE", "message": "不足"}),
        }
    )
    result = dispatch(session, "apply_leave", APPLY_ARGS, RecordingConfirm(True))
    assert result["error_code"] == "INSUFFICIENT_BALANCE"


# ---------------------------------------------------------------------------
# 確認畫面與終端機輸入
# ---------------------------------------------------------------------------


SPOOFING_REASONS = [
    "看牙醫\n  員工：E003\n  餘額：999 → 999 小時",  # 換行偽造額外列
    "看牙醫\r  員工：E003",  # 游標回行首覆蓋
    "\x1b[2J\x1b[H📝 請確認假單內容\n  員工：E003",  # ANSI 清屏＋游標定位
    "看牙醫‮3E00",  # bidi 右至左覆寫
    "看牙醫   員工：E003",  # Unicode 分行
    "看牙醫\x85  員工：E003",  # C1 NEL
    "看\t牙\x00醫​",  # tab、NUL、零寬空白
]


@pytest.mark.parametrize("reason", SPOOFING_REASONS)
def test_confirmation_cannot_be_spoofed_by_control_characters(reason):
    arguments = {**APPLY_ARGS, "employee_id": "E001", "reason": reason}
    text = format_confirmation(arguments, {"hours": 4, "remaining_before": 56, "remaining_after": 52})

    lines = text.split("\n")
    assert len(lines) == 7  # 標題＋6 列，事由不能多生出行
    assert lines[-1].startswith("  事由：")
    assert not any(agent_app.unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in text.replace("\n", ""))
    # 偽造的「員工：E003」只能以可見文字留在事由那一行，真正的員工列不受影響
    assert lines[1] == "  員工：E001"


@pytest.mark.parametrize("field", ["employee_id", "leave_type", "start_at", "end_at"])
def test_all_dynamic_fields_are_escaped(field):
    arguments = {**APPLY_ARGS, "employee_id": "E001", field: "X\n\x1b[31mFAKE"}
    text = format_confirmation(arguments, {"hours": 4, "remaining_before": 56, "remaining_after": 52})
    assert "\x1b" not in text
    assert len(text.split("\n")) == 7
    assert "X\\n\\x1b[31mFAKE" in text


def test_preview_values_are_escaped():
    text = format_confirmation({**APPLY_ARGS, "employee_id": "E001"}, {"hours": "4\n  餘額：999", "remaining_before": 1, "remaining_after": 0})
    assert len(text.split("\n")) == 7


def test_long_values_are_truncated_for_display_only():
    reason = "長" * 1000
    session = FakeSession({"preview_leave": json_result(PREVIEW_OK)})
    confirm = RecordingConfirm(True)
    dispatch(session, "apply_leave", {**APPLY_ARGS, "reason": reason}, confirm)
    assert "已截斷顯示" in confirm.prompts[0]
    assert len(confirm.prompts[0]) < 600
    assert session.calls[1][1]["reason"] == reason  # 送出的仍是完整原值


@pytest.mark.parametrize("reason", SPOOFING_REASONS)
def test_escaping_does_not_change_submitted_arguments(reason):
    session = FakeSession({"preview_leave": json_result(PREVIEW_OK)})
    confirm = RecordingConfirm(True)
    dispatch(session, "apply_leave", {**APPLY_ARGS, "reason": reason}, confirm)
    assert [args["reason"] for _, args in session.calls] == [reason, reason]
    assert len(confirm.prompts[0].split("\n")) == 7


def test_format_confirmation_without_reason_and_unknown_leave_type():
    text = format_confirmation(
        {"employee_id": "E001", "leave_type": "OTHER", "start_at": "a", "end_at": "b"},
        {"hours": 1, "remaining_before": 2, "remaining_after": 1},
    )
    assert "事由" not in text
    assert "OTHER（OTHER）" in text


@pytest.mark.parametrize(
    "answer, accepted",
    [("y", True), ("Y", True), (" yes ", True), ("YES", True), ("", False), ("n", False), ("no", False), ("yy", False), ("是", False)],
)
def test_terminal_confirm_only_accepts_explicit_yes(monkeypatch, answer, accepted):
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert terminal_confirm("假單內容") is accepted


def test_terminal_confirm_eof_means_reject(monkeypatch):
    def eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert terminal_confirm("假單內容") is False


def test_terminal_confirm_shows_summary_in_prompt(monkeypatch):
    seen = []
    monkeypatch.setattr("builtins.input", lambda prompt: seen.append(prompt) or "n")
    terminal_confirm("假單內容 XYZ")
    assert "假單內容 XYZ" in seen[0] and "y/N" in seen[0]


# ---------------------------------------------------------------------------
# 接真的 stdio Server
# ---------------------------------------------------------------------------


async def run_real(db_path, name, arguments, confirm, employee_id="E001"):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_DIR / "mcp_server.py")],
        cwd=str(PROJECT_DIR),
        env={**os.environ, "HRMS_DB_PATH": str(db_path)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            allowed = {tool["function"]["name"] for tool in agent_app.to_openai_tools((await session.list_tools()).tools)}
            return await dispatch_tool_call(
                session,
                name,
                json.dumps(arguments, ensure_ascii=False),
                employee_id=employee_id,
                allowed_tools=allowed,
                confirm=confirm,
            )


def table_counts(db):
    return {
        "requests": db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0],
        "audits": db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0],
        "e001_annual_used": db.execute(
            "SELECT used_hours FROM leave_balances WHERE employee_id='E001' AND leave_type='ANNUAL'"
        ).fetchone()[0],
    }


def test_real_server_confirmed_apply_writes(db_path, db):
    confirm = RecordingConfirm(True)
    result = asyncio.run(run_real(db_path, "apply_leave", {**APPLY_ARGS, "employee_id": "E003"}, confirm))
    assert result == {"ok": True, "request_id": 1, "hours": 4, "remaining_hours": 52}
    assert "56 → 52" in confirm.prompts[0]
    assert table_counts(db) == {"requests": 1, "audits": 1, "e001_annual_used": 28}


def test_real_server_rejected_apply_changes_nothing(db_path, db):
    before = table_counts(db)
    result = asyncio.run(run_real(db_path, "apply_leave", APPLY_ARGS, RecordingConfirm(False)))
    assert result["error_code"] == "USER_REJECTED"
    assert table_counts(db) == before


def test_real_server_insufficient_balance_never_asks(db_path, db):
    confirm = RecordingConfirm(True)
    result = asyncio.run(run_real(db_path, "apply_leave", APPLY_ARGS, confirm, employee_id="E002"))
    assert result["error_code"] == "INSUFFICIENT_BALANCE"
    assert confirm.prompts == []
    assert db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] == 0


def test_real_server_llm_cannot_call_preview_directly(db_path):
    result = asyncio.run(run_real(db_path, "preview_leave", APPLY_ARGS, RecordingConfirm(True)))
    assert result["error_code"] == "UNKNOWN_TOOL"
