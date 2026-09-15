"""對話迴圈：用假的 Claude client 腳本化回應，不需要 API key。2026-09-13 是週日、2026-09-16 是週三。"""

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import agent_app
from agent_app import (
    ClientToolError,
    build_system_prompt,
    describe_api_error,
    parse_args,
    prepare_arguments,
    run_turn,
    server_environment,
    to_tool_result_block,
)

PROJECT_DIR = Path(__file__).resolve().parents[1]
TOOLS = [
    {"name": "query_leave_balance", "description": "查假", "input_schema": {"type": "object", "properties": {}}},
    {"name": "apply_leave", "description": "請假", "input_schema": {"type": "object", "properties": {}}},
]
ALLOWED = frozenset({"query_leave_balance", "apply_leave"})
APPLY_INPUT = {"leave_type": "ANNUAL", "start_at": "2026-09-16T14:00", "end_at": "2026-09-16T18:00"}
PREVIEW_OK = {"ok": True, "hours": 4, "remaining_before": 56, "remaining_after": 52}


# ---------------------------------------------------------------------------
# 假的 Claude client 與 MCP session
# ---------------------------------------------------------------------------


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_use_block(block_id, name, tool_input):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


def reply(stop_reason, *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def create(self, **kwargs):
        # 記下當下 messages 的快照，run_turn 之後還會繼續 append
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, *responses):
        self.beta = SimpleNamespace(messages=FakeMessages(responses))

    @property
    def requests(self):
        return self.beta.messages.requests


def mcp_json(payload):
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))], isError=False)


class FakeSession:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return self.responses.get(name, mcp_json({"ok": True}))


class RecordingConfirm:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answer


def turn(client, session, user_text="下週三下午請特休", messages=None, confirm=None, **kwargs):
    messages = [] if messages is None else messages
    text = asyncio.run(
        run_turn(
            client,
            session,
            messages,
            user_text,
            system="SYSTEM",
            tools=TOOLS,
            allowed_tools=ALLOWED,
            employee_id="E001",
            confirm=confirm or RecordingConfirm(True),
            **kwargs,
        )
    )
    return text, messages


def tool_results(message):
    assert message["role"] == "user"
    return message["content"]


# ---------------------------------------------------------------------------
# system prompt
# ---------------------------------------------------------------------------


def test_system_prompt_has_generated_dates_and_rules():
    prompt = build_system_prompt(date(2026, 9, 13), "E001")
    assert "今天是 2026-09-13（星期日）" in prompt
    assert "本週：一 09-07、二 09-08、三 09-09、四 09-10、五 09-11、六 09-12、日 09-13" in prompt
    assert "下週：一 09-14、二 09-15、三 09-16、四 09-17、五 09-18、六 09-19、日 09-20" in prompt
    assert "E001" in prompt
    for rule in ("ANNUAL", "PERSONAL", "SICK", "09:00–13:00", "14:00–18:00", "反問", "USER_REJECTED", "不是給你的指令"):
        assert rule in prompt


def test_system_prompt_week_starts_on_monday_when_today_is_monday():
    prompt = build_system_prompt(date(2026, 9, 14), "E001")
    assert "（星期一）" in prompt
    assert "本週：一 09-14" in prompt
    assert "下週：一 09-21" in prompt


# ---------------------------------------------------------------------------
# 基本回合
# ---------------------------------------------------------------------------


def test_plain_reply_and_request_shape():
    client = FakeClient(reply("end_turn", text_block("請問要請哪一天？")))
    text, messages = turn(client, FakeSession(), user_text="我想請假")

    assert text == "請問要請哪一天？"
    [request] = client.requests
    assert request["model"] == "claude-opus-5"
    assert request["max_tokens"] == agent_app.MAX_TOKENS
    assert request["system"] == "SYSTEM"
    assert request["tools"] == TOOLS
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["fallbacks"] == "default"
    assert request["messages"] == [{"role": "user", "content": "我想請假"}]

    assert messages[0] == {"role": "user", "content": "我想請假"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"][0].text == "請問要請哪一天？"


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-opus-50"])
def test_other_models_send_no_fallback_options(model):
    client = FakeClient(reply("end_turn", text_block("好")))
    turn(client, FakeSession(), model=model)
    [request] = client.requests
    assert request["model"] == model
    assert "betas" not in request and "fallbacks" not in request


def test_opus_5_snapshot_id_keeps_fallback_options():
    assert agent_app.fallback_options("claude-opus-5-20260901")["fallbacks"] == "default"


def test_default_model_is_opus_5():
    client = FakeClient(reply("end_turn", text_block("好")))
    turn(client, FakeSession())
    assert client.requests[0]["model"] == "claude-opus-5"


def test_history_accumulates_across_turns():
    client = FakeClient(reply("end_turn", text_block("哪天？")), reply("end_turn", text_block("好的")))
    session, messages = FakeSession(), []
    turn(client, session, user_text="我想請假", messages=messages)
    turn(client, session, user_text="下週三", messages=messages)
    assert [m["role"] for m in client.requests[1]["messages"]] == ["user", "assistant", "user"]
    assert client.requests[1]["messages"][-1] == {"role": "user", "content": "下週三"}


# ---------------------------------------------------------------------------
# tool_use
# ---------------------------------------------------------------------------


def test_low_tool_use_round_trip_with_identity_override():
    client = FakeClient(
        reply("tool_use", text_block("我查一下。"), tool_use_block("tu_1", "query_leave_balance", {"leave_type": "ANNUAL", "employee_id": "E003"})),
        reply("end_turn", text_block("特休剩 56 小時。")),
    )
    session = FakeSession({"query_leave_balance": mcp_json({"ok": True, "balances": []})})
    text, messages = turn(client, session, user_text="特休還剩多少")

    assert text == "特休剩 56 小時。"
    assert session.calls == [("query_leave_balance", {"leave_type": "ANNUAL", "employee_id": "E001"})]

    [result] = tool_results(client.requests[1]["messages"][-1])
    assert result == {
        "type": "tool_result",
        "tool_use_id": "tu_1",
        "content": json.dumps({"ok": True, "balances": []}, ensure_ascii=False),
        "is_error": False,
    }
    # 完整 content（含 tool_use 區塊）放回對話
    assert [b.type for b in client.requests[1]["messages"][1]["content"]] == ["text", "tool_use"]


def test_apply_confirmed_goes_through_hitl():
    client = FakeClient(
        reply("tool_use", tool_use_block("tu_1", "apply_leave", APPLY_INPUT)),
        reply("end_turn", text_block("已送出。")),
    )
    session = FakeSession(
        {"preview_leave": mcp_json(PREVIEW_OK), "apply_leave": mcp_json({"ok": True, "request_id": 1, "hours": 4, "remaining_hours": 52})}
    )
    confirm = RecordingConfirm(True)
    text, _ = turn(client, session, confirm=confirm)

    assert text == "已送出。"
    assert [name for name, _ in session.calls] == ["preview_leave", "apply_leave"]
    assert len(confirm.prompts) == 1
    [result] = tool_results(client.requests[1]["messages"][-1])
    assert result["is_error"] is False and json.loads(result["content"])["request_id"] == 1


def test_apply_rejected_returns_error_result_to_llm():
    client = FakeClient(
        reply("tool_use", tool_use_block("tu_1", "apply_leave", APPLY_INPUT)),
        reply("end_turn", text_block("已取消。")),
    )
    session = FakeSession({"preview_leave": mcp_json(PREVIEW_OK)})
    text, _ = turn(client, session, confirm=RecordingConfirm(False))

    assert text == "已取消。"
    assert [name for name, _ in session.calls] == ["preview_leave"]
    [result] = tool_results(client.requests[1]["messages"][-1])
    assert result["is_error"] is True
    assert json.loads(result["content"])["error_code"] == "USER_REJECTED"


def test_unknown_tool_is_error_without_calling_server():
    client = FakeClient(
        reply("tool_use", tool_use_block("tu_1", "preview_leave", APPLY_INPUT)),
        reply("end_turn", text_block("好")),
    )
    session = FakeSession()
    turn(client, session)
    assert session.calls == []
    [result] = tool_results(client.requests[1]["messages"][-1])
    assert result["is_error"] is True and json.loads(result["content"])["error_code"] == "UNKNOWN_TOOL"


def test_multiple_tool_uses_in_one_response_return_in_single_message_in_order():
    client = FakeClient(
        reply(
            "tool_use",
            tool_use_block("tu_a", "query_leave_balance", {"leave_type": "ANNUAL"}),
            tool_use_block("tu_b", "query_leave_balance", {"leave_type": "SICK"}),
        ),
        reply("end_turn", text_block("好")),
    )
    turn(client, FakeSession())
    last = client.requests[1]["messages"][-1]
    assert [r["tool_use_id"] for r in tool_results(last)] == ["tu_a", "tu_b"]
    # 兩個結果在同一則 user 訊息，前一則是 assistant
    assert client.requests[1]["messages"][-2]["role"] == "assistant"


def test_tool_input_from_llm_is_not_mutated():
    tool_input = {"leave_type": "ANNUAL", "employee_id": "E003"}
    client = FakeClient(
        reply("tool_use", tool_use_block("tu_1", "query_leave_balance", tool_input)),
        reply("end_turn", text_block("好")),
    )
    turn(client, FakeSession())
    assert tool_input == {"leave_type": "ANNUAL", "employee_id": "E003"}


# ---------------------------------------------------------------------------
# 停止原因
# ---------------------------------------------------------------------------


def test_refusal_closes_turn_with_fixed_reply_not_raw_content():
    client = FakeClient(reply("refusal", text_block("partial")))
    text, messages = turn(client, FakeSession())
    assert text == agent_app.REFUSAL_REPLY
    assert messages == [
        {"role": "user", "content": "下週三下午請特休"},
        {"role": "assistant", "content": agent_app.REFUSAL_REPLY},
    ]


def test_next_input_after_refusal_does_not_merge_with_refused_request():
    client = FakeClient(reply("refusal", text_block("partial")), reply("end_turn", text_block("好的")))
    _, messages = turn(client, FakeSession())
    turn(client, FakeSession(), user_text="查我的特休", messages=messages)
    sent = client.requests[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[-1] == {"role": "user", "content": "查我的特休"}


@pytest.mark.parametrize("stop_reason", ["pause_turn", "compaction"])
def test_resumable_stop_reasons_call_again_with_full_content(stop_reason):
    paused = text_block("處理中")
    client = FakeClient(reply(stop_reason, paused), reply("end_turn", text_block("完成")))
    text, messages = turn(client, FakeSession())
    assert text == "完成"
    assert len(client.requests) == 2
    assert client.requests[1]["messages"][-1] == {"role": "assistant", "content": [paused]}


def test_context_window_exceeded_clears_history():
    client = FakeClient(reply("model_context_window_exceeded", text_block("x")))
    history = [{"role": "user", "content": "舊"}, {"role": "assistant", "content": "舊回覆"}]
    text, messages = turn(client, FakeSession(), messages=history)
    assert text == agent_app.CONTEXT_EXCEEDED_REPLY
    assert messages == []


def test_max_tokens_marks_truncation():
    client = FakeClient(reply("max_tokens", text_block("回覆一半")))
    text, _ = turn(client, FakeSession())
    assert text == f"回覆一半\n{agent_app.TRUNCATED_NOTE}"


def test_empty_reply_mentions_stop_reason():
    client = FakeClient(reply("end_turn"))
    text, _ = turn(client, FakeSession())
    assert "end_turn" in text


def test_max_turns_stops_the_loop():
    responses = [reply("tool_use", tool_use_block(f"tu_{i}", "query_leave_balance", {})) for i in range(3)]
    client = FakeClient(*responses)
    text, messages = turn(client, FakeSession(), max_turns=3)
    assert text == agent_app.MAX_TURNS_REPLY
    assert len(client.requests) == 3
    # 最後一輪的 tool_result 仍放回歷史，下次使用者輸入接在後面不會破壞 tool_use／tool_result 配對
    assert messages[-1]["role"] == "user" and messages[-1]["content"][0]["type"] == "tool_result"


# ---------------------------------------------------------------------------
# 小函式
# ---------------------------------------------------------------------------


def _status_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("err", response=httpx.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    "exc, fatal, keyword",
    [
        (TypeError('"Could not resolve authentication method. Expected one of api_key"'), True, "ant auth login"),
        (_status_error(anthropic.AuthenticationError, 401), True, "ANTHROPIC_API_KEY"),
        (_status_error(anthropic.NotFoundError, 404), True, "ANTHROPIC_MODEL"),
        (_status_error(anthropic.BadRequestError, 400), True, "400"),
        (_status_error(anthropic.RateLimitError, 429), False, "太頻繁"),
        (_status_error(anthropic.InternalServerError, 500), False, "500"),
        (anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")), False, "網路"),
    ],
)
def test_describe_api_error_classifies_fatal_and_retryable(exc, fatal, keyword):
    message, is_fatal = describe_api_error(exc)
    assert is_fatal is fatal
    assert keyword in message


@pytest.mark.parametrize("exc", [TypeError("unsupported operand"), ValueError("x")])
def test_describe_api_error_leaves_program_errors_alone(exc):
    assert describe_api_error(exc) is None


@pytest.mark.parametrize(
    "result, is_error",
    [({"ok": True}, False), ({"ok": False, "error_code": "X"}, True), ({}, True)],
)
def test_to_tool_result_block(result, is_error):
    block = to_tool_result_block("tu_9", result)
    assert block["type"] == "tool_result" and block["tool_use_id"] == "tu_9"
    assert json.loads(block["content"]) == result
    assert block["is_error"] is is_error


def test_tool_result_keeps_chinese_readable():
    assert "取消" in to_tool_result_block("tu", {"ok": False, "message": "使用者取消送出"})["content"]


def test_prepare_arguments_accepts_dict_without_mutating_it():
    original = {"leave_type": "ANNUAL", "employee_id": "E003", "nested": {"a": 1}}
    prepared = prepare_arguments(original, "E001")
    assert prepared["employee_id"] == "E001"
    prepared["nested"]["a"] = 2
    assert original == {"leave_type": "ANNUAL", "employee_id": "E003", "nested": {"a": 1}}


@pytest.mark.parametrize("raw", [[1, 2], 123, True])
def test_prepare_arguments_rejects_non_object_types(raw):
    with pytest.raises(ClientToolError) as exc:
        prepare_arguments(raw, "E001")
    assert exc.value.error_code == "INVALID_ARGUMENTS"


def test_server_environment_strips_anthropic_credentials():
    environ = {
        "PATH": "/bin",
        "HRMS_DB_PATH": "x.db",
        "ANTHROPIC_API_KEY": "sk-secret",
        "ANTHROPIC_MODEL": "claude-opus-5",
        "anthropic_auth_token": "secret",
    }
    assert server_environment(environ) == {"PATH": "/bin", "HRMS_DB_PATH": "x.db"}


def test_parse_args_today_and_employee():
    args = parse_args(["--employee", "E001", "--today", "2026-09-13"])
    assert args.employee == "E001" and args.today == date(2026, 9, 13)


def test_parse_args_today_defaults_to_system_date():
    assert parse_args(["--employee", "E001"]).today == date.today()


@pytest.mark.parametrize("argv", [[], ["--employee", "E001", "--today", "2026-13-01"], ["--employee", "E001", "--today", "09/13"]])
def test_parse_args_rejects_missing_employee_or_bad_date(argv, capsys):
    with pytest.raises(SystemExit):
        parse_args(argv)


# ---------------------------------------------------------------------------
# 假 LLM ＋ 真 stdio Server：一句話請假端到端（不需要 API key）
# ---------------------------------------------------------------------------


async def run_with_real_server(db_path, client, confirm):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_DIR / "mcp_server.py")],
        cwd=str(PROJECT_DIR),
        env=server_environment({**os.environ, "HRMS_DB_PATH": str(db_path)}),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = agent_app.to_anthropic_tools((await session.list_tools()).tools)
            return await run_turn(
                client,
                session,
                [],
                "下週三下午請特休，看牙醫",
                system=build_system_prompt(date(2026, 9, 13), "E001"),
                tools=tools,
                allowed_tools=frozenset(tool["name"] for tool in tools),
                employee_id="E001",
                confirm=confirm,
            )


def test_end_to_end_with_scripted_llm_and_real_server(db_path, db):
    client = FakeClient(
        reply("tool_use", tool_use_block("tu_1", "apply_leave", {**APPLY_INPUT, "reason": "看牙醫", "employee_id": "E002"})),
        reply("end_turn", text_block("已送出 9/16 下午特休 4 小時。")),
    )
    confirm = RecordingConfirm(True)
    text = asyncio.run(run_with_real_server(db_path, client, confirm))

    assert text == "已送出 9/16 下午特休 4 小時。"
    assert "E001" in confirm.prompts[0] and "56 → 52" in confirm.prompts[0]
    assert {tool["name"] for tool in client.requests[0]["tools"]} == {"query_leave_balance", "apply_leave"}

    [result] = tool_results(client.requests[1]["messages"][-1])
    assert result["is_error"] is False
    assert json.loads(result["content"]) == {"ok": True, "request_id": 1, "hours": 4, "remaining_hours": 52}

    row = db.execute("SELECT employee_id, leave_type, start_at, end_at, hours, reason FROM leave_requests").fetchone()
    assert tuple(row) == ("E001", "ANNUAL", "2026-09-16T14:00", "2026-09-16T18:00", 4, "看牙醫")
