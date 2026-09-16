"""Gradio demo worker 的生命週期（任務 #6 里程碑 1）：真 MCP stdio、逾時、關閉、PID 檔強制終止。

時序靠 threading.Event 與 Future，不用 sleep 猜；等外部行程時才用有上限的輪詢。
"""

import asyncio
import concurrent.futures
import gc
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

import demo_worker
from agent_app import build_system_prompt, call_tool_safely, run_turn
from demo_worker import AgentWorker, WorkerConfig, force_kill_from_pid_file, process_exists
from mcp.client.stdio import stdio_client
from test_agent_loop import APPLY_INPUT, FakeClient, reply, text_block, tool_use_block

TESTS_DIR = Path(__file__).resolve().parent
STUBBORN_SERVER = TESTS_DIR / "stubborn_server.py"
WAIT = 30  # 等待真實子行程的上限（秒）


def wait_until(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def fast_config(**overrides):
    values = dict(startup_timeout=15, tool_timeout=15, shutdown_grace=5, cancel_grace=2, join_timeout=5)
    values.update(overrides)
    return WorkerConfig(**values)


@pytest.fixture
def worker_factory():
    workers = []

    def make(config, **kwargs):
        worker = AgentWorker(config, **kwargs)
        worker.start()
        workers.append(worker)
        return worker

    yield make
    for worker in workers:
        worker.stop()


class ConfirmGate:
    """async confirm：記下 prompt 與 PID，等測試執行緒決定。"""

    def __init__(self, worker):
        self.worker = worker
        self.asked = threading.Event()
        self.future = None
        self.prompt = None
        self.pid = None

    async def __call__(self, prompt):
        self.future = asyncio.get_running_loop().create_future()
        self.prompt = prompt
        [pid_file] = self.worker.active_pid_files()
        self.pid = demo_worker.read_pid_file(pid_file)
        self.asked.set()
        return await self.future

    def answer(self, approved):
        def resolve():
            if self.future is not None and not self.future.done():
                self.future.set_result(approved)

        self.worker.call_soon(resolve)


def leave_turn(worker, client, confirm):
    async def body(session, tools):
        return await run_turn(
            client,
            session,
            [],
            "下週三下午請特休",
            system=build_system_prompt(date(2026, 9, 13), "E001"),
            tools=tools,
            allowed_tools=frozenset(tool["name"] for tool in tools),
            employee_id="E001",
            confirm=confirm,
        )

    return worker.submit(worker.run_with_mcp(body))


# ---------------------------------------------------------------------------
# 真 MCP stdio
# ---------------------------------------------------------------------------


def test_check_ready_lists_llm_tools_and_cleans_up(worker_factory, db_path):
    worker = worker_factory(fast_config(db_path=db_path))
    names = worker.submit(worker.check_ready()).result(timeout=WAIT)
    assert sorted(names) == ["apply_leave", "query_leave_balance"]
    assert worker.active_pid_files() == []


def test_real_stdio_turn_writes_db_and_server_exits(worker_factory, db_path, db):
    worker = worker_factory(fast_config(db_path=db_path))
    client = FakeClient(
        reply("tool_use", tool_use_block("tu_1", "apply_leave", {**APPLY_INPUT, "employee_id": "E002"})),
        reply("end_turn", text_block("已送出")),
    )
    gate = ConfirmGate(worker)
    future = leave_turn(worker, client, gate)

    assert gate.asked.wait(WAIT)
    assert "E001" in gate.prompt and "56 → 52" in gate.prompt
    assert gate.pid is not None and process_exists(gate.pid)
    gate.answer(True)

    assert future.result(timeout=WAIT) == "已送出"
    row = db.execute("SELECT employee_id, start_at, hours FROM leave_requests").fetchone()
    assert tuple(row) == ("E001", "2026-09-16T14:00", 4)
    assert worker.active_pid_files() == []
    assert wait_until(lambda: not process_exists(gate.pid))


def test_stop_while_awaiting_confirm_rejects_and_joins(db_path, db):
    worker = AgentWorker(fast_config(db_path=db_path))
    worker.start()
    client = FakeClient(
        reply("tool_use", tool_use_block("tu_1", "apply_leave", APPLY_INPUT)),
        reply("end_turn", text_block("已取消")),
    )
    gate = ConfirmGate(worker)
    future = leave_turn(worker, client, gate)
    assert gate.asked.wait(WAIT)

    def reject_pending():
        if gate.future is not None and not gate.future.done():
            gate.future.set_result(False)

    assert worker.stop(before_stop=reject_pending) is True
    assert future.result(timeout=1) == "已取消"
    assert db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] == 0
    assert not worker.is_alive
    assert wait_until(lambda: not process_exists(gate.pid))
    with pytest.raises(RuntimeError):
        worker.submit(worker.check_ready())


def test_stop_cancels_turn_that_ignores_before_stop(db_path):
    """before_stop 沒解決確認：寬限逾時後取消 turn，連線仍關閉、執行緒可 join。"""
    worker = AgentWorker(fast_config(db_path=db_path, shutdown_grace=0.5, cancel_grace=5))
    worker.start()
    client = FakeClient(reply("tool_use", tool_use_block("tu_1", "apply_leave", APPLY_INPUT)))
    gate = ConfirmGate(worker)
    future = leave_turn(worker, client, gate)
    assert gate.asked.wait(WAIT)

    assert worker.stop() is True
    assert future.cancelled()
    assert worker.active_pid_files() == []
    assert wait_until(lambda: not process_exists(gate.pid))


# ---------------------------------------------------------------------------
# 逾時
# ---------------------------------------------------------------------------


def test_startup_timeout_against_server_that_never_answers(worker_factory):
    async def open_stubborn(stack, pid_file, config):
        params = demo_worker.server_parameters(pid_file, config)
        params = params.model_copy(update={"args": [str(STUBBORN_SERVER), str(pid_file)]})
        await stack.enter_async_context(stdio_client(params))
        await asyncio.Event().wait()  # 永遠等不到 initialize 回應

    worker = worker_factory(fast_config(startup_timeout=3), open_session=open_stubborn)
    seen = {}

    async def body(session, tools):
        raise AssertionError("不應該進到 body")

    async def run():
        task = asyncio.ensure_future(worker.run_with_mcp(body))
        while not worker.active_pid_files():
            await asyncio.sleep(0.01)
        [pid_file] = worker.active_pid_files()
        seen["pid_file"] = pid_file
        while demo_worker.read_pid_file(pid_file) is None and not task.done():
            await asyncio.sleep(0.01)
        seen["pid"] = demo_worker.read_pid_file(pid_file)
        return await task

    future = worker.submit(run())
    with pytest.raises(TimeoutError):
        future.result(timeout=WAIT)
    assert seen["pid"] is not None
    assert wait_until(lambda: not process_exists(seen["pid"]))
    assert worker.active_pid_files() == []
    assert not seen["pid_file"].exists()


def test_startup_hook_that_hangs_raises_timeout(worker_factory):
    async def hang(stack, pid_file, config):
        await asyncio.Event().wait()

    worker = worker_factory(fast_config(startup_timeout=0.2), open_session=hang)

    async def body(session, tools):
        raise AssertionError("不應該進到 body")

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        worker.submit(worker.run_with_mcp(body)).result(timeout=WAIT)
    assert time.monotonic() - started < 5


def test_tool_call_timeout_becomes_tool_call_error():
    class SlowSession:
        async def call_tool(self, name, arguments=None):
            await asyncio.sleep(10)

    session = demo_worker.TimeoutSession(SlowSession(), timeout=0.05)
    result = asyncio.run(call_tool_safely(session, "query_leave_balance", {"employee_id": "E001"}))
    assert result["ok"] is False and result["error_code"] == "TOOL_CALL_ERROR"


# ---------------------------------------------------------------------------
# 取消不是硬上限：PID 檔強制終止
# ---------------------------------------------------------------------------


def pid_file_path(tmp_path):
    # 檔名要帶 PID_FILE_PREFIX＋唯一值，身分驗證靠命令列裡的檔名
    return tmp_path / f"{demo_worker.PID_FILE_PREFIX}{time.monotonic_ns()}.pid"


def spawn_pid_writer(pid_file, delay=None, wait_for_file=True):
    args = [sys.executable, str(STUBBORN_SERVER), str(pid_file)]
    if delay is not None:
        args.append(str(delay))
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL)
    if wait_for_file:
        assert wait_until(lambda: demo_worker.read_pid_file(pid_file) is not None)
    return proc


def cleanup_process(proc, server_pid=None):
    for pid in (server_pid, proc.pid if proc else None):
        if pid is not None and process_exists(pid):
            demo_worker.kill_process_tree(pid)


def test_force_kill_from_pid_file_terminates_process(tmp_path):
    # Windows 的 venv python.exe 是轉接程式，會再啟動真正的直譯器子行程：
    # Popen.pid 是轉接程式，PID 檔記的是真正的 Server。這正是需要 PID 檔的原因。
    pid_file = pid_file_path(tmp_path)
    proc = spawn_pid_writer(pid_file)
    server_pid = demo_worker.read_pid_file(pid_file)
    try:
        assert process_exists(server_pid)
        result = force_kill_from_pid_file(pid_file)
        assert result == demo_worker.KillResult(server_pid, "killed")
        assert not process_exists(server_pid)
        assert not pid_file.exists()
        proc.wait(timeout=WAIT)  # 真正的 Server 被終止後，轉接程式也結束
    finally:
        cleanup_process(proc, server_pid)


@pytest.mark.parametrize(
    "content, status",
    [(None, "no_pid_file"), ("", "invalid"), ("abc", "invalid"), ("0", "invalid"), ("-5", "invalid")],
)
def test_force_kill_handles_missing_or_invalid_pid_file(tmp_path, content, status):
    pid_file = pid_file_path(tmp_path)
    if content is not None:
        pid_file.write_text(content, encoding="ascii")
    result = force_kill_from_pid_file(pid_file)
    assert result.status == status
    assert result.server_gone is (status == "invalid")
    assert not pid_file.exists()


def test_force_kill_of_already_exited_process_is_harmless(tmp_path):
    pid_file = pid_file_path(tmp_path)
    proc = spawn_pid_writer(pid_file)
    server_pid = demo_worker.read_pid_file(pid_file)
    demo_worker.kill_process_tree(proc.pid)  # 連同真正的 Server 一起結束
    proc.wait(timeout=WAIT)
    assert wait_until(lambda: not process_exists(server_pid))
    assert force_kill_from_pid_file(pid_file) == demo_worker.KillResult(server_pid, "already_gone")
    assert not pid_file.exists()


def test_reused_pid_of_unrelated_process_is_not_killed(tmp_path):
    """PID 檔指向的行程命令列不含這輪的 PID 檔名：視為 PID 重用，不終止。"""
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)
    try:
        assert wait_until(lambda: process_exists(unrelated.pid))
        pid_file = pid_file_path(tmp_path)
        pid_file.write_text(str(unrelated.pid), encoding="ascii")
        result = force_kill_from_pid_file(pid_file)
        assert result == demo_worker.KillResult(unrelated.pid, "not_ours")
        assert unrelated.poll() is None and process_exists(unrelated.pid)
        assert not pid_file.exists()
    finally:
        cleanup_process(unrelated)


def test_kill_that_does_not_take_effect_keeps_pid_file(tmp_path, monkeypatch):
    pid_file = pid_file_path(tmp_path)
    proc = spawn_pid_writer(pid_file)
    server_pid = demo_worker.read_pid_file(pid_file)
    try:
        monkeypatch.setattr(demo_worker, "kill_process_tree", lambda pid: False)
        result = force_kill_from_pid_file(pid_file, verify_timeout=0.3)
        assert result == demo_worker.KillResult(server_pid, "failed")
        assert not result.server_gone
        assert pid_file.exists() and process_exists(server_pid)
        monkeypatch.undo()
        assert force_kill_from_pid_file(pid_file).status == "killed"
        assert not pid_file.exists()
    finally:
        cleanup_process(proc, server_pid)


def test_concurrent_force_kill_calls_never_raise(tmp_path):
    pid_file = pid_file_path(tmp_path)
    proc = spawn_pid_writer(pid_file)
    server_pid = demo_worker.read_pid_file(pid_file)
    errors, results = [], []
    barrier = threading.Barrier(8)

    def call():
        barrier.wait()
        try:
            results.append(force_kill_from_pid_file(pid_file))
        except Exception as exc:  # noqa: BLE001 - 就是要確認沒有任何例外
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(8)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT)
        assert errors == []
        assert all(result.server_gone or result.status == "no_pid_file" for result in results)
        assert [result.status for result in results].count("killed") == 1
        assert not pid_file.exists()
        assert not process_exists(server_pid)
    finally:
        cleanup_process(proc, server_pid)


class StuckTransport:
    """__aexit__ 不理會取消，直到 Server 行程（轉接程式）真的結束。模擬 cleanup 卡住。"""

    def __init__(self, proc):
        self.proc = proc
        self.exited_at = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        waiter = asyncio.ensure_future(asyncio.to_thread(self.proc.wait))
        while True:
            try:
                await asyncio.shield(waiter)
                self.exited_at = time.monotonic()
                return False
            except asyncio.CancelledError:
                if waiter.cancelled():  # loop 關閉時連 waiter 一起取消：不能再重試，否則空轉佔住 GIL
                    raise
                continue


def stuck_opener(records, delay=None):
    async def open_stuck(stack, pid_file, config):
        proc = await asyncio.to_thread(spawn_pid_writer, pid_file, delay, delay is None)
        transport = await stack.enter_async_context(StuckTransport(proc))
        records.append({"proc": proc, "transport": transport, "pid_file": pid_file})
        return object(), []

    return open_stuck


def server_pid_of(record):
    assert wait_until(lambda: demo_worker.read_pid_file(record["pid_file"]) is not None or record["proc"].poll() is not None)
    return demo_worker.read_pid_file(record["pid_file"])


def test_stuck_cleanup_is_force_killed_after_both_deadlines(worker_factory):
    records = []
    config = fast_config(shutdown_grace=0.3, cancel_grace=0.3)
    worker = worker_factory(config, open_session=stuck_opener(records))
    pids = []

    async def body(session, tools):
        pids.append(demo_worker.read_pid_file(records[0]["pid_file"]))
        return "done"

    started = time.monotonic()
    try:
        # 截止取消被收回，body 的回傳值保留
        assert worker.submit(worker.run_with_mcp(body)).result(timeout=WAIT) == "done"
        record = records[0]
        assert not process_exists(pids[0])
        assert record["proc"].poll() is not None
        # 至少等過兩段截止才強制終止
        assert record["transport"].exited_at - started >= config.shutdown_grace + config.cancel_grace
        assert worker.active_pid_files() == []
        assert not record["pid_file"].exists()
    finally:
        for record in records:
            cleanup_process(record["proc"], pids[0] if pids else None)


def test_pid_file_written_late_is_still_force_killed(worker_factory):
    """強制終止計時器到期時 PID 檔還沒出現：持續重試，檔案出現後仍會終止 Server。"""
    records = []
    config = fast_config(shutdown_grace=0.2, cancel_grace=0.2)
    worker = worker_factory(config, open_session=stuck_opener(records, delay=1.5))

    async def body(session, tools):
        return "done"

    try:
        assert worker.submit(worker.run_with_mcp(body)).result(timeout=WAIT) == "done"
        record = records[0]
        assert record["proc"].poll() is not None
        assert not record["pid_file"].exists()
        assert worker.active_pid_files() == []
    finally:
        for record in records:
            cleanup_process(record["proc"])


def test_stop_force_kills_server_when_turn_ignores_cancel():
    """關閉流程：turn 不理會取消時，依 PID 檔強制終止並驗證後，worker 仍可 join。"""
    records = []
    entered = threading.Event()

    async def body(session, tools):
        entered.set()
        waiter = asyncio.ensure_future(asyncio.to_thread(records[0]["proc"].wait))
        while True:  # 不理會取消，直到 Server 被終止
            try:
                return await asyncio.shield(waiter)
            except asyncio.CancelledError:
                if waiter.cancelled():  # loop 關閉時連 waiter 一起取消：不能再重試，否則空轉佔住 GIL
                    raise
                continue

    worker = AgentWorker(fast_config(shutdown_grace=0.3, cancel_grace=1), open_session=stuck_opener(records))
    worker.start()
    server_pid = None
    try:
        worker.submit(worker.run_with_mcp(body))
        assert entered.wait(WAIT)
        server_pid = server_pid_of(records[0])
        assert worker.stop() is True
        assert not process_exists(server_pid)
        assert records[0]["proc"].poll() is not None
    finally:
        for record in records:
            cleanup_process(record["proc"], server_pid)


def test_stop_after_external_future_cancel_still_waits_for_cleanup():
    """對外 Future.cancel() 不代表 loop 內工作結束：stop() 仍要等 cleanup 或強制終止後才回報成功。"""
    records = []
    entered = threading.Event()

    async def body(session, tools):
        entered.set()
        await asyncio.Event().wait()

    worker = AgentWorker(fast_config(shutdown_grace=0.3, cancel_grace=1), open_session=stuck_opener(records))
    worker.start()
    server_pid = None
    try:
        future = worker.submit(worker.run_with_mcp(body))
        assert entered.wait(WAIT)
        server_pid = server_pid_of(records[0])
        future.cancel()
        assert worker.stop() is True
        assert not process_exists(server_pid)
        assert worker.active_pid_files() == []
    finally:
        for record in records:
            cleanup_process(record["proc"], server_pid)


def test_future_cancelled_before_start_is_settled_without_running():
    worker = AgentWorker(fast_config())
    worker.start()
    ran = []
    blocker = threading.Event()
    try:
        worker.call_soon(blocker.wait, WAIT)  # 暫時卡住 loop，讓下一個工作還沒開始

        async def job():
            ran.append(True)

        future = worker.submit(job())
        assert future.cancel() is True
        blocker.set()
        assert worker.stop() is True
        assert ran == []
    finally:
        blocker.set()


# 故意讓強殺失敗：工作永遠不會結束，loop 關閉時留下未完成的 coroutine，GC 時的警告是這個情境的預期結果
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_stop_reports_failure_when_server_cannot_be_killed(monkeypatch):
    records = []
    entered = threading.Event()

    async def body(session, tools):
        entered.set()
        waiter = asyncio.ensure_future(asyncio.to_thread(records[0]["proc"].wait))
        while True:
            try:
                return await asyncio.shield(waiter)
            except asyncio.CancelledError:
                if waiter.cancelled():  # loop 關閉時連 waiter 一起取消：不能再重試，否則空轉佔住 GIL
                    raise
                continue

    monkeypatch.setattr(demo_worker, "KILL_VERIFY_TIMEOUT", 0.2)
    worker = AgentWorker(
        fast_config(shutdown_grace=0.2, cancel_grace=0.3, join_timeout=1), open_session=stuck_opener(records)
    )
    worker.start()
    server_pid = None
    try:
        worker.submit(worker.run_with_mcp(body))
        assert entered.wait(WAIT)
        server_pid = server_pid_of(records[0])
        monkeypatch.setattr(demo_worker, "kill_process_tree", lambda pid: False)
        assert worker.stop() is False
        assert process_exists(server_pid)
        assert records[0]["pid_file"].exists()
    finally:
        monkeypatch.undo()
        for record in records:
            cleanup_process(record["proc"], server_pid)
            demo_worker.remove_pid_file(record["pid_file"])
        # 在本測試內回收留下的 coroutine，警告才會被上面的 filterwarnings 吃掉，不會跑到別的測試
        gc.collect()


class SlowButCancellableTransport:
    """__aexit__ 很慢但會響應取消。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await asyncio.sleep(30)
        return False


def slow_opener(records, transport_cls=SlowButCancellableTransport):
    async def open_slow(stack, pid_file, config):
        proc = await asyncio.to_thread(spawn_pid_writer, pid_file)
        records.append({"proc": proc, "pid_file": pid_file, "server_pid": demo_worker.read_pid_file(pid_file)})
        await stack.enter_async_context(transport_cls())
        return object(), []

    return open_slow


def test_slow_cleanup_keeps_original_exception_and_kills_server(worker_factory):
    records = []
    worker = worker_factory(fast_config(shutdown_grace=0.3, cancel_grace=5), open_session=slow_opener(records))

    async def body(session, tools):
        raise ValueError("body failed")

    try:
        future = worker.submit(worker.run_with_mcp(body))
        # 關閉逾時被取消後，拋出的仍是 body 的 ValueError，而不是 CancelledError
        with pytest.raises(ValueError, match="body failed"):
            future.result(timeout=WAIT)
        assert not process_exists(records[0]["server_pid"])
        assert worker.active_pid_files() == []
    finally:
        for record in records:
            cleanup_process(record["proc"], record["server_pid"])


def test_external_cancel_during_slow_cleanup_still_cancels(worker_factory):
    """關閉流程要求的取消不能被截止邏輯吞掉。"""
    records = []
    closing = threading.Event()

    class SignalingSlowTransport(SlowButCancellableTransport):
        async def __aexit__(self, *exc_info):
            closing.set()
            return await super().__aexit__(*exc_info)

    worker = worker_factory(
        fast_config(shutdown_grace=30, cancel_grace=30), open_session=slow_opener(records, SignalingSlowTransport)
    )

    async def body(session, tools):
        return "done"

    try:
        future = worker.submit(worker.run_with_mcp(body))
        assert closing.wait(WAIT)
        worker.call_soon(worker._cancel_tasks)
        with pytest.raises(concurrent.futures.CancelledError):
            future.result(timeout=WAIT)
        assert not process_exists(records[0]["server_pid"])
    finally:
        for record in records:
            cleanup_process(record["proc"], record["server_pid"])


def test_deadline_and_external_cancel_both_swallowed_by_cleanup_still_cancels(worker_factory):
    """截止取消與外部取消都被 cleanup 吞掉後正常返回：只收回截止那一次，外部取消仍往上拋。"""
    records = []
    first_cancel = threading.Event()

    class SwallowTwiceTransport:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            swallowed = 0
            while swallowed < 2:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    swallowed += 1
                    first_cancel.set()
            return False

    worker = worker_factory(
        fast_config(shutdown_grace=0.2, cancel_grace=30), open_session=slow_opener(records, SwallowTwiceTransport)
    )

    async def body(session, tools):
        return "done"

    try:
        future = worker.submit(worker.run_with_mcp(body))
        assert first_cancel.wait(WAIT)  # 截止取消已被吞掉
        worker.call_soon(worker._cancel_tasks)  # 外部取消
        with pytest.raises(concurrent.futures.CancelledError):
            future.result(timeout=WAIT)
    finally:
        for record in records:
            cleanup_process(record["proc"], record["server_pid"])
            demo_worker.remove_pid_file(record["pid_file"])


@pytest.mark.parametrize(
    "command_line, expected",
    [
        (r'C:\venv\python.exe demo_server_launcher.py C:\Temp\hrms-demo-server-abc.pid', True),
        (r'"C:\venv\python.exe" "C:\Temp\hrms-demo-server-abc.pid"', True),
        (r"python launcher.py c:/temp/HRMS-DEMO-SERVER-ABC.PID", True),
        (r"python launcher.py C:\Temp\hrms-demo-server-abc.pid.bak", False),
        (r"python launcher.py C:\Temp\old-hrms-demo-server-abc.pid", False),
        (r"python launcher.py D:\Temp\hrms-demo-server-abc.pid", False),
        ("python -c sleep", False),
    ],
)
def test_command_line_argument_must_match_whole_pid_file_path(command_line, expected):
    assert demo_worker.command_line_has_argument(command_line, r"C:\Temp\hrms-demo-server-abc.pid") is expected


def test_force_kill_timer_does_not_block_worker_loop(worker_factory, monkeypatch):
    """強制終止的阻塞查詢在執行緒跑：查詢期間 loop 仍能處理其他 callback。"""
    records = []
    query_started = threading.Event()
    real_command_line = demo_worker.process_command_line

    def slow_command_line(pid):
        query_started.set()
        time.sleep(2)
        return real_command_line(pid)

    monkeypatch.setattr(demo_worker, "process_command_line", slow_command_line)
    worker = worker_factory(fast_config(shutdown_grace=0.1, cancel_grace=0.1), open_session=stuck_opener(records))

    async def body(session, tools):
        return "done"

    server_pid = None
    try:
        future = worker.submit(worker.run_with_mcp(body))
        assert query_started.wait(WAIT)
        server_pid = server_pid_of(records[0])
        responded = threading.Event()
        sent = time.monotonic()
        worker.call_soon(responded.set)
        assert responded.wait(WAIT)
        assert time.monotonic() - sent < 1.0  # 慢查詢要 2 秒；loop 沒被卡住才會這麼快回應
        assert future.result(timeout=WAIT) == "done"
        assert not process_exists(server_pid)
    finally:
        monkeypatch.undo()
        for record in records:
            cleanup_process(record["proc"], server_pid)


def test_stop_first_sweep_before_pid_file_exists_still_kills_server():
    """stop() 第一次 sweep 時 PID 檔還沒寫出：路徑要保留，最後一次 sweep 仍會終止 Server。"""
    records = []
    entered = threading.Event()

    async def body(session, tools):
        entered.set()
        waiter = asyncio.ensure_future(asyncio.to_thread(records[0]["proc"].wait))
        while True:
            try:
                return await asyncio.shield(waiter)
            except asyncio.CancelledError:
                if waiter.cancelled():
                    raise
                continue

    config = fast_config(shutdown_grace=0.2, cancel_grace=1.5)
    worker = AgentWorker(config, open_session=stuck_opener(records, delay=2.5))
    worker.start()
    try:
        worker.submit(worker.run_with_mcp(body))
        assert entered.wait(WAIT)
        assert not records[0]["pid_file"].exists()  # 還沒寫出
        assert worker.stop() is True
        assert records[0]["proc"].poll() is not None
        assert not records[0]["pid_file"].exists()
    finally:
        for record in records:
            server_pid = demo_worker.read_pid_file(record["pid_file"])
            cleanup_process(record["proc"], server_pid)
            demo_worker.remove_pid_file(record["pid_file"])


def test_stop_reports_failure_when_jobs_finished_but_server_survives(monkeypatch):
    """工作都結束了，但 cleanup 被中斷且強殺無效：Server 還活著，stop() 必須回 False。"""
    records = []
    monkeypatch.setattr(demo_worker, "KILL_VERIFY_TIMEOUT", 0.2)
    monkeypatch.setattr(demo_worker, "kill_process_tree", lambda pid: False)
    worker = AgentWorker(fast_config(shutdown_grace=0.2, cancel_grace=0.3), open_session=slow_opener(records))
    worker.start()

    async def body(session, tools):
        return "done"

    try:
        future = worker.submit(worker.run_with_mcp(body))
        assert future.result(timeout=WAIT) == "done"  # 工作本身已結束
        [pid_file] = worker.active_pid_files()  # 強殺失敗，PID 檔留著等 stop() 再處理
        assert worker.stop() is False
        assert process_exists(records[0]["server_pid"])
        assert pid_file.exists()
    finally:
        monkeypatch.undo()
        for record in records:
            cleanup_process(record["proc"], record["server_pid"])
            demo_worker.remove_pid_file(record["pid_file"])
