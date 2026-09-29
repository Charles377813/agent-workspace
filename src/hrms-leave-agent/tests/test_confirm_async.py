"""dispatch_tool_call 的 confirm 可以是同步或 async；只有 True 算同意（任務 #6）。"""

import asyncio

import pytest

from agent_app import dispatch_tool_call
from test_client_hitl import APPLY_ARGS, ALLOWED, FakeSession


def run_apply(confirm):
    session = FakeSession()
    result = asyncio.run(
        dispatch_tool_call(
            session, "apply_leave", dict(APPLY_ARGS), employee_id="E001", allowed_tools=ALLOWED, confirm=confirm
        )
    )
    return result, [name for name, _ in session.calls]


def test_sync_true_submits():
    result, calls = run_apply(lambda prompt: True)
    assert result["ok"] is True
    assert calls == ["preview_leave", "apply_leave"]


def test_async_true_submits():
    async def confirm(prompt):
        await asyncio.sleep(0)
        return True

    result, calls = run_apply(confirm)
    assert result["ok"] is True
    assert calls == ["preview_leave", "apply_leave"]


def test_async_false_rejects():
    async def confirm(prompt):
        return False

    result, calls = run_apply(confirm)
    assert result["error_code"] == "USER_REJECTED"
    assert calls == ["preview_leave"]


@pytest.mark.parametrize("answer", ["y", 1, "True", [True], None])
def test_non_true_values_reject(answer):
    result, calls = run_apply(lambda prompt: answer)
    assert result["error_code"] == "USER_REJECTED"
    assert calls == ["preview_leave"]


@pytest.mark.parametrize("answer", ["y", 1])
def test_async_non_true_values_reject(answer):
    async def confirm(prompt):
        return answer

    result, calls = run_apply(confirm)
    assert result["error_code"] == "USER_REJECTED"
    assert calls == ["preview_leave"]


def test_async_confirm_waits_on_future_resolved_later():
    """模擬 Gradio：confirm 建立 Future 等按鈕，由另一個 callback 解決。"""

    async def scenario():
        loop = asyncio.get_running_loop()
        seen = []

        async def confirm(prompt):
            future = loop.create_future()
            seen.append(prompt)
            loop.call_soon(future.set_result, True)
            return await future

        session = FakeSession()
        result = await dispatch_tool_call(
            session, "apply_leave", dict(APPLY_ARGS), employee_id="E001", allowed_tools=ALLOWED, confirm=confirm
        )
        return result, seen, session

    result, seen, session = asyncio.run(scenario())
    assert result["ok"] is True
    assert len(seen) == 1 and "E001" in seen[0]
    assert session.calls[0][1] == session.calls[1][1]
