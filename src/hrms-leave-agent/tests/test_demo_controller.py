"""DemoController（任務 #6 里程碑 2）：狀態機、view_version 入場控制、確認流程、錯誤分類與回滾。

用假 Claude client＋假 MCP session（不開真的 stdio），時序靠 threading.Event／Barrier。
"""

import asyncio
import json
import threading
import time
from datetime import date

import anthropic
import httpx
import pytest

import agent_app
from demo_controller import DemoController, ErrorKind, State, classify_turn_error
from demo_worker import AgentWorker, WorkerConfig
from test_agent_loop import (
    APPLY_INPUT,
    PREVIEW_OK,
    TOOLS,
    FakeSession,
    _status_error,
    mcp_json,
    reply,
    text_block,
    tool_use_block,
)

WAIT = 10


class Gate:
    """腳本步驟：等測試放行後才回應，用來讓 controller 停在 BUSY。"""

    def __init__(self, then):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.then = then


class ScriptedClient:
    """假 Claude client：依序回應、拋例外或在 Gate 等待。"""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []
        self.beta = self
        self.messages = self

    async def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        step = self.steps.pop(0)
        if isinstance(step, Gate):
            step.entered.set()
            await asyncio.to_thread(step.release.wait, WAIT)
            step = step.then
        if isinstance(step, BaseException):
            raise step
        return step


@pytest.fixture
def make_controller():
    controllers = []

    def make(client, session=None, *, employee_id="E001", open_session=None):
        session = session or FakeSession({"preview_leave": mcp_json(PREVIEW_OK)})

        async def open_fake(stack, pid_file, config):
            return session, TOOLS

        worker = AgentWorker(
            WorkerConfig(startup_timeout=5, tool_timeout=5, shutdown_grace=2, cancel_grace=1, join_timeout=2),
            open_session=open_session or open_fake,
        )
        worker.start()
        controller = DemoController(worker, client, today=date(2026, 9, 13), employee_id=employee_id)
        controller.session = session
        controllers.append(controller)
        return controller

    yield make
    for controller in controllers:
        controller.close()


def settle(controller, view):
    return controller.wait_until_settled(view.version, timeout=WAIT)


def send(controller, text="下週三下午請特休"):
    outcome = controller.send(controller.view().version, text)
    assert outcome.accepted
    assert outcome.view.state == State.BUSY
    return settle(controller, outcome.view)


def apply_client(final_text="已送出"):
    return ScriptedClient(
        reply("tool_use", tool_use_block("tu_1", "apply_leave", {**APPLY_INPUT, "employee_id": "E002"})),
        reply("end_turn", text_block(final_text)),
    )


# ---------------------------------------------------------------------------
# 基本流程
# ---------------------------------------------------------------------------


def test_plain_reply_returns_to_idle_with_chat(make_controller):
    controller = make_controller(ScriptedClient(reply("end_turn", text_block("請問要請哪一天？"))))
    start = controller.view()
    assert start.state == State.IDLE and start.version == 0

    view = send(controller, "我想請假")
    assert view.state == State.IDLE
    assert view.chat == (("user", "我想請假"), ("assistant", "請問要請哪一天？"))
    assert view.version > start.version


def test_apply_waits_for_confirm_then_submits_same_frozen_arguments(make_controller):
    client = apply_client()
    controller = make_controller(client)

    view = send(controller)
    assert view.state == State.AWAIT_CONFIRM
    assert "E001" in view.confirm_text and "56 → 52" in view.confirm_text
    assert [name for name, _ in controller.session.calls] == ["preview_leave"]

    outcome = controller.answer(view.version, True)
    assert outcome.accepted and outcome.view.state == State.BUSY and outcome.view.confirm_text == ""
    view = settle(controller, outcome.view)

    assert view.state == State.IDLE
    assert view.chat[-1] == ("assistant", "已送出")
    calls = controller.session.calls
    assert [name for name, _ in calls] == ["preview_leave", "apply_leave"]
    assert calls[0][1] == calls[1][1] and calls[1][1]["employee_id"] == "E001"


def test_cancel_returns_user_rejected_to_llm(make_controller):
    client = apply_client("已取消")
    controller = make_controller(client)
    view = send(controller)

    view = settle(controller, controller.answer(view.version, False).view)
    assert view.state == State.IDLE
    assert [name for name, _ in controller.session.calls] == ["preview_leave"]
    [result] = client.requests[1]["messages"][-1]["content"]
    assert json.loads(result["content"])["error_code"] == "USER_REJECTED"


def test_failed_preview_never_asks_for_confirmation(make_controller):
    session = FakeSession({"preview_leave": mcp_json({"ok": False, "error_code": "INSUFFICIENT_BALANCE"})})
    controller = make_controller(apply_client("特休不足"), session)
    view = send(controller)
    assert view.state == State.IDLE
    assert view.confirm_text == ""
    assert view.chat[-1] == ("assistant", "特休不足")


# ---------------------------------------------------------------------------
# 入場控制
# ---------------------------------------------------------------------------


def test_stale_or_invalid_send_is_rejected_without_change(make_controller):
    controller = make_controller(ScriptedClient())
    before = controller.view()
    for version, text in [(before.version + 1, "hi"), (before.version - 1, "hi"), (before.version, "   ")]:
        outcome = controller.send(version, text)
        assert not outcome.accepted
        assert outcome.view == before


def test_too_long_input_is_rejected_with_notice(make_controller):
    controller = make_controller(ScriptedClient())
    outcome = controller.send(0, "請" * 501)
    assert not outcome.accepted
    assert outcome.view.state == State.IDLE and "上限" in outcome.view.notice


def test_two_sends_with_same_version_only_one_accepted(make_controller):
    gate = Gate(reply("end_turn", text_block("好")))
    controller = make_controller(ScriptedClient(gate))
    version = controller.view().version
    barrier = threading.Barrier(2)
    outcomes = []

    def press(text):
        barrier.wait()
        outcomes.append(controller.send(version, text))

    threads = [threading.Thread(target=press, args=(text,)) for text in ("第一", "第二")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)

    assert sorted(outcome.accepted for outcome in outcomes) == [False, True]
    assert gate.entered.wait(WAIT)
    gate.release.set()
    view = controller.wait_until_settled(version + 1, timeout=WAIT)
    assert view.state == State.IDLE
    assert [role for role, _ in view.chat] == ["user", "assistant"]


def test_send_queued_during_busy_is_rejected_after_idle(make_controller):
    """模擬 Gradio 排隊：BUSY 時按的送出帶舊版本，輪到時已回 IDLE 仍被拒。"""
    gate = Gate(reply("end_turn", text_block("好")))
    controller = make_controller(ScriptedClient(gate))
    first = controller.send(0, "第一句")
    assert first.accepted
    queued_version = first.view.version  # 使用者在 BUSY 畫面上又按了一次

    assert gate.entered.wait(WAIT)
    gate.release.set()
    idle = settle(controller, first.view)
    assert idle.state == State.IDLE

    outcome = controller.send(queued_version, "排隊的第二句")
    assert not outcome.accepted
    assert outcome.view == idle


def test_double_confirm_only_submits_once(make_controller):
    controller = make_controller(apply_client())
    view = send(controller)
    barrier = threading.Barrier(2)
    outcomes = []

    def press():
        barrier.wait()
        outcomes.append(controller.answer(view.version, True))

    threads = [threading.Thread(target=press) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)

    assert sorted(outcome.accepted for outcome in outcomes) == [False, True]
    settle(controller, [o for o in outcomes if o.accepted][0].view)
    assert [name for name, _ in controller.session.calls].count("apply_leave") == 1


def test_answer_rejected_when_not_awaiting_or_stale(make_controller):
    controller = make_controller(apply_client())
    assert not controller.answer(0, True).accepted  # IDLE

    view = send(controller)
    assert not controller.answer(view.version - 1, True).accepted  # 舊版本
    assert controller.view().state == State.AWAIT_CONFIRM

    accepted = controller.answer(view.version, False)
    assert accepted.accepted
    assert not controller.answer(view.version, True).accepted  # 連點的第二下
    settle(controller, accepted.view)
    assert [name for name, _ in controller.session.calls] == ["preview_leave"]


def test_switch_employee_only_when_idle_with_current_version(make_controller):
    gate = Gate(reply("end_turn", text_block("好")))
    controller = make_controller(ScriptedClient(gate, reply("end_turn", text_block("E002 你好"))))
    busy = controller.send(0, "你好")
    assert busy.accepted
    assert not controller.switch_employee(busy.view.version, "E002").accepted  # BUSY

    assert gate.entered.wait(WAIT)
    gate.release.set()
    idle = settle(controller, busy.view)
    assert not controller.switch_employee(busy.view.version, "E002").accepted  # BUSY 時排隊的切換
    assert not controller.switch_employee(idle.version, "E999").accepted  # 未知員工

    switched = controller.switch_employee(idle.version, "E002")
    assert switched.accepted
    assert switched.view.employee_id == "E002" and switched.view.chat == ()

    view = send(controller, "你好")
    assert view.chat[-1] == ("assistant", "E002 你好")
    last_request = controller._client.requests[-1]
    assert last_request["messages"] == [{"role": "user", "content": "你好"}]  # 歷史已清空
    assert "E002" in last_request["system"]


def test_switch_employee_rejected_while_awaiting_confirm(make_controller):
    controller = make_controller(apply_client())
    view = send(controller)
    assert not controller.switch_employee(view.version, "E002").accepted
    assert controller.view().state == State.AWAIT_CONFIRM


# ---------------------------------------------------------------------------
# 錯誤分類與回滾
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, kind",
    [
        (TypeError(f'"{agent_app.AUTH_RESOLVE_ERROR}. Expected one of api_key"'), ErrorKind.FATAL),
        (_status_error(anthropic.AuthenticationError, 401), ErrorKind.FATAL),
        (_status_error(anthropic.NotFoundError, 404), ErrorKind.FATAL),
        (_status_error(anthropic.BadRequestError, 400), ErrorKind.RESET),
        (_status_error(anthropic.RateLimitError, 429), ErrorKind.RECOVERABLE),
        (_status_error(anthropic.InternalServerError, 500), ErrorKind.RECOVERABLE),
        (TimeoutError(), ErrorKind.RECOVERABLE),
        (ValueError("bug"), ErrorKind.RECOVERABLE),
        (TypeError("unsupported operand"), ErrorKind.RECOVERABLE),
    ],
)
def test_classify_turn_error(exc, kind):
    assert classify_turn_error(exc)[0] == kind


def test_recoverable_error_rolls_back_partial_tool_use_history(make_controller):
    """第二次呼叫 LLM 失敗：本輪的 user／tool_use／tool_result 全部回滾，下一輪不會帶半套歷史。"""
    client = ScriptedClient(
        reply("end_turn", text_block("你好")),
        reply("tool_use", tool_use_block("tu_1", "query_leave_balance", {})),
        _status_error(anthropic.RateLimitError, 429),
        reply("end_turn", text_block("剩 56 小時")),
    )
    controller = make_controller(client)
    send(controller, "你好")
    history_after_first_turn = list(controller._messages)

    view = send(controller, "查特休")
    assert view.state == State.IDLE
    assert "太頻繁" in view.notice and view.chat[-1][1].startswith("⚠️")
    assert controller._messages == history_after_first_turn

    send(controller, "查特休")
    retry_messages = client.requests[-1]["messages"]
    assert retry_messages[:-1] == history_after_first_turn
    assert retry_messages[-1] == {"role": "user", "content": "查特休"}


def test_bad_request_clears_history_and_chat(make_controller):
    client = ScriptedClient(
        reply("end_turn", text_block("你好")),
        _status_error(anthropic.BadRequestError, 400),
    )
    controller = make_controller(client)
    send(controller, "你好")
    view = send(controller, "再一句")
    assert view.state == State.IDLE
    assert controller._messages == []
    assert len(view.chat) == 1 and "已重置" in view.chat[0][1]


def test_fatal_error_locks_controller(make_controller):
    controller = make_controller(ScriptedClient(_status_error(anthropic.AuthenticationError, 401)))
    view = send(controller, "你好")
    assert view.state == State.FATAL
    assert "API key" in view.notice
    assert not controller.send(view.version, "再試").accepted
    assert not controller.switch_employee(view.version, "E002").accepted


def test_startup_failure_is_recoverable(make_controller):
    async def broken(stack, pid_file, config):
        raise ConnectionError("server did not start")

    controller = make_controller(ScriptedClient(), open_session=broken)
    view = send(controller, "你好")
    assert view.state == State.IDLE
    assert controller._messages == []
    assert "再試一次" in view.notice


def test_error_after_confirm_rolls_back_and_clears_confirm(make_controller):
    client = ScriptedClient(
        reply("tool_use", tool_use_block("tu_1", "apply_leave", APPLY_INPUT)),
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")),
    )
    controller = make_controller(client)
    view = send(controller)
    view = settle(controller, controller.answer(view.version, True).view)
    assert view.state == State.IDLE
    assert view.confirm_text == ""
    assert controller._messages == []
    assert "網路" in view.notice


# ---------------------------------------------------------------------------
# 關閉
# ---------------------------------------------------------------------------


def test_close_while_awaiting_confirm_rejects_and_blocks_further_actions(make_controller):
    controller = make_controller(apply_client("已取消"))
    view = send(controller)
    assert view.state == State.AWAIT_CONFIRM

    assert controller.close() is True
    closed = controller.view()
    assert closed.state == State.CLOSING
    assert [name for name, _ in controller.session.calls] == ["preview_leave"]
    assert not controller.answer(closed.version, True).accepted
    assert not controller.send(closed.version, "你好").accepted


def test_wait_until_settled_returns_immediately_when_already_settled(make_controller):
    controller = make_controller(ScriptedClient(reply("end_turn", text_block("好"))))
    outcome = controller.send(0, "你好")
    final = settle(controller, outcome.view)
    again = controller.wait_until_settled(outcome.view.version, timeout=0.1)
    assert again == final


def test_unknown_initial_employee_rejected():
    with pytest.raises(ValueError):
        DemoController(AgentWorker(), ScriptedClient(), today=date(2026, 9, 13), employee_id="E999")


def test_end_to_end_with_real_mcp_server_writes_db(db_path, db):
    """controller＋真的 MCP stdio Server＋SQLite：送出 → 確認 → 寫入，身分仍是 E001。"""
    worker = AgentWorker(WorkerConfig(startup_timeout=20, tool_timeout=20, shutdown_grace=5, cancel_grace=2, db_path=db_path))
    worker.start()
    controller = DemoController(worker, apply_client("已送出"), today=date(2026, 9, 13))
    try:
        outcome = controller.send(0, "下週三下午請特休")
        view = controller.wait_until_settled(outcome.view.version, timeout=30)
        assert view.state == State.AWAIT_CONFIRM
        assert "56 → 52" in view.confirm_text
        assert db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] == 0

        view = controller.wait_until_settled(controller.answer(view.version, True).view.version, timeout=30)
        assert view.state == State.IDLE and view.chat[-1] == ("assistant", "已送出")
        row = db.execute("SELECT employee_id, start_at, end_at, hours FROM leave_requests").fetchone()
        assert tuple(row) == ("E001", "2026-09-16T14:00", "2026-09-16T18:00", 4)
    finally:
        assert controller.close() is True


# ---------------------------------------------------------------------------
# Codex review 里程碑 2＋變異測試補強
# ---------------------------------------------------------------------------


def test_answer_bumps_version(make_controller):
    controller = make_controller(apply_client())
    view = send(controller)
    outcome = controller.answer(view.version, True)
    assert outcome.view.version > view.version
    settle(controller, outcome.view)


def test_second_confirmation_in_same_turn(make_controller):
    """同一輪 LLM 連續兩次 apply_leave：每次都重新進 AWAIT_CONFIRM，各自可回答。"""
    second_input = {**APPLY_INPUT, "start_at": "2026-09-17T09:00", "end_at": "2026-09-17T13:00"}
    client = ScriptedClient(
        reply("tool_use", tool_use_block("tu_1", "apply_leave", APPLY_INPUT)),
        reply("tool_use", tool_use_block("tu_2", "apply_leave", second_input)),
        reply("end_turn", text_block("兩筆都處理完")),
    )
    controller = make_controller(client)

    first = send(controller)
    assert first.state == State.AWAIT_CONFIRM and "2026-09-16T14:00" in first.confirm_text
    second = settle(controller, controller.answer(first.version, True).view)
    assert second.state == State.AWAIT_CONFIRM and "2026-09-17T09:00" in second.confirm_text
    assert not controller.answer(first.version, True).accepted  # 第一次確認畫面的舊點擊

    done = settle(controller, controller.answer(second.version, False).view)
    assert done.state == State.IDLE and done.chat[-1] == ("assistant", "兩筆都處理完")
    assert [name for name, _ in controller.session.calls] == ["preview_leave", "apply_leave", "preview_leave"]


def test_answer_when_worker_cannot_schedule_goes_fatal(make_controller):
    controller = make_controller(apply_client())
    view = send(controller)

    def broken_call_soon(*args):
        raise RuntimeError("Event loop is closed")

    controller._worker.call_soon = broken_call_soon
    outcome = controller.answer(view.version, True)
    assert not outcome.accepted
    assert outcome.view.state == State.FATAL and outcome.view.confirm_text == ""
    assert "重新啟動" in outcome.view.notice
    assert outcome.view.version > view.version
    del controller._worker.call_soon  # 還原，讓 fixture 可以正常關閉


def test_cancelled_turn_rolls_back_and_returns_to_idle(make_controller):
    gate = Gate(reply("end_turn", text_block("不會用到")))
    client = ScriptedClient(reply("end_turn", text_block("你好")), gate)
    controller = make_controller(client)
    send(controller, "你好")
    history = list(controller._messages)

    busy = controller.send(controller.view().version, "第二句")
    assert busy.accepted and gate.entered.wait(WAIT)
    controller._worker.call_soon(controller._worker._cancel_tasks)
    view = settle(controller, busy.view)
    gate.release.set()

    assert view.state == State.IDLE
    assert controller._messages == history
    assert "中止" in view.notice


def test_close_while_awaiting_confirm_lets_llm_see_user_rejected(make_controller):
    """close() 先以「取消」解決確認：LLM 收到 USER_REJECTED 並正常收尾，而不是 turn 被強制取消。"""
    client = apply_client("已取消")
    controller = make_controller(client)
    view = send(controller)
    assert view.state == State.AWAIT_CONFIRM

    assert controller.close() is True
    assert len(client.requests) == 2
    [result] = client.requests[1]["messages"][-1]["content"]
    assert json.loads(result["content"])["error_code"] == "USER_REJECTED"


def test_late_resolve_for_another_turn_is_ignored(make_controller):
    controller = make_controller(apply_client())
    view = send(controller)
    other_turn = controller._pending[0] + 1

    done = threading.Event()
    controller._worker.call_soon(controller._resolve_pending, other_turn, True)
    controller._worker.call_soon(done.set)
    assert done.wait(WAIT)

    still = controller.view()
    assert still.state == State.AWAIT_CONFIRM and still.version == view.version
    assert [name for name, _ in controller.session.calls] == ["preview_leave"]
    settle(controller, controller.answer(view.version, False).view)


def test_wait_until_settled_keeps_waiting_while_busy(make_controller):
    gate = Gate(reply("end_turn", text_block("好")))
    controller = make_controller(ScriptedClient(gate))
    busy = controller.send(0, "你好")
    assert busy.accepted and gate.entered.wait(WAIT)

    started = time.monotonic()
    view = controller.wait_until_settled(0, timeout=0.3)  # 版本已前進，但仍是 BUSY
    assert view.state == State.BUSY
    assert time.monotonic() - started >= 0.25

    gate.release.set()
    assert settle(controller, busy.view).state == State.IDLE


def test_close_closes_client_inside_worker_loop(make_controller):
    closed_in = []

    class ClosableClient(ScriptedClient):
        async def close(self):
            closed_in.append(threading.current_thread().name)

    controller = make_controller(ClosableClient(reply("end_turn", text_block("好"))))
    send(controller, "你好")
    assert controller.close() is True
    assert closed_in == ["hrms-demo-worker"]
