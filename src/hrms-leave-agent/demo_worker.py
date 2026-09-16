"""Gradio demo 的背景 worker（任務 #6 里程碑 1）。

- 背景執行緒＋獨立 asyncio event loop：跨兩次按鈕事件保住「暫停中的 turn」，不依賴 Gradio 的 loop。
- 每輪重開 MCP stdio（經 demo_server_launcher.py，Server 會寫 PID 檔）。
- 分段逾時：啟動（connect → initialize → list_tools）、工具呼叫、關閉。
- 取消不是硬上限：cleanup 卡住時，由 loop 上的計時器（或關閉流程）依 PID 檔強制終止 Server 行程樹，
  終止前驗證行程身分、終止後確認行程已消失。

設計見 docs/tasks/06-hrms-gradio-demo.md §4。
"""

import asyncio
import concurrent.futures
import functools
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_app import PROJECT_DIR, server_environment, to_anthropic_tools

logger = logging.getLogger(__name__)

LAUNCHER_PATH = PROJECT_DIR / "demo_server_launcher.py"
PID_FILE_PREFIX = "hrms-demo-server-"
# 強制終止後等行程真的消失的上限
KILL_VERIFY_TIMEOUT = 5.0
# PID 檔還沒出現或終止失敗時，計時器重試的間隔
KILL_RETRY_INTERVAL = 0.5

T = TypeVar("T")


@dataclass(frozen=True)
class WorkerConfig:
    startup_timeout: float = 30.0
    tool_timeout: float = 60.0
    # 關閉的兩段截止：先等 cleanup，逾時取消；取消後再等 cancel_grace，仍未結束就強制終止
    shutdown_grace: float = 10.0
    cancel_grace: float = 5.0
    join_timeout: float = 5.0
    db_path: str | os.PathLike | None = None


# ---------------------------------------------------------------------------
# 子行程強制終止
# ---------------------------------------------------------------------------


def process_exists(pid: int) -> bool:
    if os.name == "nt":
        # Windows 的 os.kill(pid, 0) 會直接 TerminateProcess，不能拿來探測
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return f'"{pid}"' in output
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_command_line(pid: int) -> str | None:
    """行程的命令列；查不到（行程不存在或查詢失敗）回 None。"""
    try:
        if os.name == "nt":
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
            text = result.stdout.strip()
            return text or None
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        if proc_cmdline.exists():
            return proc_cmdline.read_bytes().replace(b"\0", b" ").decode(errors="replace").strip() or None
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=15, check=False
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def command_line_has_argument(command_line: str, argument: str) -> bool:
    """命令列裡是否有一個「完整等於」argument 的參數（不分大小寫、斜線方向），`.pid.bak` 之類不算。"""

    def normalize(text: str) -> str:
        return text.replace("\\", "/").lower()

    boundary_before = r"(?:^|[\s\"'])"
    boundary_after = r"(?:$|[\s\"'])"
    pattern = boundary_before + re.escape(normalize(argument)) + boundary_after
    return re.search(pattern, normalize(command_line)) is not None


def is_server_for_pid_file(pid: int, pid_file: Path) -> bool | None:
    """PID 是否仍是當初寫這個 PID 檔的 Server（命令列有一個參數完整等於這輪專屬的 PID 檔路徑）。

    回 None 代表無法判斷（查不到命令列但行程還在）。用來避免 PID 被重用時誤殺無關行程。
    """
    command_line = process_command_line(pid)
    if command_line is None:
        return False if not process_exists(pid) else None
    return command_line_has_argument(command_line, str(pid_file))


def kill_process_tree(pid: int) -> bool:
    """送出強制終止 pid 與其子行程的指令；回傳指令是否成功送出（行程已不存在也算）。"""
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return result.returncode == 0 or not process_exists(pid)
    try:
        # stdio_client 在 POSIX 用 start_new_session，Server 自成 process group（pgid == pid）；
        # 只有確定是它自己的 group 才整組終止，否則只終止該 PID，避免誤殺無關的 group
        if os.getpgid(pid) == pid and pid != os.getpgrp():
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


def wait_process_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not process_exists(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def read_pid_file(pid_file: Path) -> int | None:
    try:
        text = pid_file.read_text(encoding="ascii").strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 and pid != os.getpid() else None


# 關閉流程（主執行緒）與 loop 上的截止計時器可能同時處理同一個 PID 檔；
# Windows 上一邊讀檔一邊刪檔會 PermissionError，所以整段序列化
_pid_file_lock = threading.Lock()


def remove_pid_file(pid_file: Path) -> None:
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        logger.warning("刪除 PID 檔失敗：%s", pid_file, exc_info=True)


@dataclass(frozen=True)
class KillResult:
    pid: int | None
    # no_pid_file：PID 檔還沒出現（Server 可能尚未啟動），不算完成
    # invalid：PID 檔內容無效，已刪除
    # already_gone：行程已不存在
    # not_ours：PID 已被其他行程重用，原 Server 已不存在，不終止
    # killed：已終止並確認消失
    # failed：無法判斷身分或終止後仍存在，PID 檔保留以便重試
    status: str

    @property
    def server_gone(self) -> bool:
        return self.status in {"invalid", "already_gone", "not_ours", "killed"}


def force_kill_from_pid_file(pid_file: Path, verify_timeout: float | None = None) -> KillResult:
    """依 PID 檔強制終止 Server：先驗證身分，終止後確認行程消失才刪 PID 檔。

    只在 cleanup 沒有正常完成時呼叫。從不拋例外：它跑在關閉流程與計時器 callback 裡，拋出會中斷關閉。
    """
    verify_timeout = KILL_VERIFY_TIMEOUT if verify_timeout is None else verify_timeout
    with _pid_file_lock:
        try:
            if not pid_file.exists():
                return KillResult(None, "no_pid_file")
            pid = read_pid_file(pid_file)
            if pid is None:
                remove_pid_file(pid_file)
                return KillResult(None, "invalid")
            if not process_exists(pid):
                remove_pid_file(pid_file)
                return KillResult(pid, "already_gone")
            identity = is_server_for_pid_file(pid, pid_file)
            if identity is False:
                if process_exists(pid):
                    logger.warning("PID %s 已不是這輪的 MCP Server（PID 重用），不終止", pid)
                    remove_pid_file(pid_file)
                    return KillResult(pid, "not_ours")
                remove_pid_file(pid_file)
                return KillResult(pid, "already_gone")
            if identity is None:
                logger.error("無法確認 PID %s 的身分，不終止；PID 檔保留：%s", pid, pid_file)
                return KillResult(pid, "failed")

            logger.warning("MCP Server 沒有正常關閉，強制終止 PID %s", pid)
            kill_process_tree(pid)
            if wait_process_gone(pid, verify_timeout):
                remove_pid_file(pid_file)
                return KillResult(pid, "killed")
            logger.error("強制終止後 PID %s 仍存在；PID 檔保留：%s", pid, pid_file)
            return KillResult(pid, "failed")
        except Exception:  # noqa: BLE001 - 見 docstring：絕不能中斷關閉流程
            logger.exception("強制終止 MCP Server 時發生錯誤：%s", pid_file)
            return KillResult(None, "failed")


class ForceKillTimer:
    """loop 上的強制終止計時器：到期後依 PID 檔終止；PID 檔還沒出現或終止失敗就重試，直到被取消。

    查命令列、taskkill、等行程消失都是阻塞呼叫，放到執行緒跑，不卡住 worker loop
    （loop 被卡住時，被強殺後本來可以結束的 cleanup 與外部取消都無法進行）。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, pid_file: Path, delay: float):
        self._loop = loop
        self._pid_file = pid_file
        self._task: asyncio.Task | None = None
        self._handle = loop.call_later(delay, self._start)

    def _start(self) -> None:
        self._task = self._loop.create_task(self._run())

    async def _run(self) -> None:
        while True:
            result = await asyncio.to_thread(force_kill_from_pid_file, self._pid_file)
            if result.status not in {"no_pid_file", "failed"}:
                return
            await asyncio.sleep(KILL_RETRY_INTERVAL)

    def cancel(self) -> None:
        self._handle.cancel()
        if self._task is not None:
            self._task.cancel()


# ---------------------------------------------------------------------------
# MCP 連線
# ---------------------------------------------------------------------------


class TimeoutSession:
    """包住 MCP ClientSession，call_tool 加逾時。

    逾時的 TimeoutError 會被 agent_app.call_tool_safely 轉成 TOOL_CALL_ERROR 交回 LLM，turn 照常繼續。
    """

    def __init__(self, session, timeout: float):
        self._session = session
        self._timeout = timeout

    async def call_tool(self, name: str, arguments: dict | None = None):
        return await asyncio.wait_for(self._session.call_tool(name, arguments), self._timeout)


def server_parameters(pid_file: Path, config: WorkerConfig) -> StdioServerParameters:
    environ = dict(os.environ)
    if config.db_path is not None:
        environ["HRMS_DB_PATH"] = str(config.db_path)
    return StdioServerParameters(
        command=sys.executable,
        args=[str(LAUNCHER_PATH), str(pid_file)],
        cwd=str(PROJECT_DIR),
        env=server_environment(environ),
    )


async def open_stdio_session(stack: AsyncExitStack, pid_file: Path, config: WorkerConfig):
    """開 stdio → initialize → list_tools，回傳 (session, 給 LLM 的 tools)。context 都掛在 stack 上。"""
    read, write = await stack.enter_async_context(stdio_client(server_parameters(pid_file, config)))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    tools = to_anthropic_tools((await session.list_tools()).tools)
    return session, tools


OpenSession = Callable[[AsyncExitStack, Path, WorkerConfig], Awaitable[tuple[Any, list[dict]]]]
McpBody = Callable[[Any, list[dict]], Awaitable[T]]


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class _Job:
    """一個 submit 的工作。完成與否以 loop 內的 coroutine 真正結束為準，不看對外 Future。"""

    __slots__ = ("started", "finished", "coroutine")

    def __init__(self, coroutine):
        self.started = False
        self.finished = False
        self.coroutine = coroutine


class AgentWorker:
    """背景執行緒＋獨立 event loop。submit 的 coroutine 都在這個 loop 上跑。"""

    def __init__(self, config: WorkerConfig | None = None, *, open_session: OpenSession = open_stdio_session):
        self.config = config or WorkerConfig()
        self._open_session = open_session
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._jobs_changed = threading.Condition(self._lock)
        self._closed = False
        self._active_jobs = 0
        self._tasks: set[asyncio.Task] = set()  # 只在 loop 執行緒內存取
        self._pid_files: set[Path] = set()

    # --- 生命週期 -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("worker 已經啟動過")
        self._thread = threading.Thread(target=self._run_loop, name="hrms-demo-worker", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("worker event loop 沒有啟動")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            leftovers = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in leftovers:
                task.cancel()
            if leftovers:
                loop.run_until_complete(asyncio.wait(leftovers, timeout=self.config.cancel_grace))
            loop.close()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, before_stop: Callable[[], None] | None = None) -> bool:
        """有序關閉，回傳是否完全乾淨：工作都結束、Server 都確認消失、worker 執行緒已結束。

        1. 拒收新工作；2. 在 loop 內執行 before_stop（例如以 False 解決等待中的確認）；
        3. 等進行中的工作 → 逾時取消 → 再逾時就依 PID 檔強制終止 Server；
        4. 對仍登記的 PID 檔做最後一次強制終止並驗證；5. 停 loop、join。
        """
        with self._lock:
            if self._closed:
                return not self.is_alive
            self._closed = True
        loop = self._loop
        if loop is None or self._thread is None:
            return True

        if before_stop is not None:
            loop.call_soon_threadsafe(before_stop)

        config = self.config
        jobs_done = self._wait_jobs(config.shutdown_grace)
        if not jobs_done:
            loop.call_soon_threadsafe(self._cancel_tasks)
            jobs_done = self._wait_jobs(config.cancel_grace)
        if not jobs_done:
            # 工作還在跑：PID 檔可能還沒寫出，no_pid_file 的路徑要留給最後一次 sweep
            self._sweep_pid_files(final=False)
            jobs_done = self._wait_jobs(config.cancel_grace)

        servers_gone = self._sweep_pid_files(final=True)
        if not jobs_done and servers_gone:
            # 最後一次強殺可能剛讓卡住的工作解開
            jobs_done = self._wait_jobs(config.cancel_grace)
        if not jobs_done:
            logger.error("關閉時仍有 %d 個工作沒有結束", self._active_jobs)

        loop.call_soon_threadsafe(loop.stop)
        self._thread.join(config.join_timeout)
        thread_done = not self._thread.is_alive()
        if not thread_done:
            logger.error("worker 執行緒在 %.1f 秒內沒有結束", config.join_timeout)
        return jobs_done and servers_gone and thread_done

    def _wait_jobs(self, timeout: float) -> bool:
        with self._jobs_changed:
            return self._jobs_changed.wait_for(lambda: self._active_jobs == 0, timeout)

    def _sweep_pid_files(self, *, final: bool) -> bool:
        all_gone = True
        for pid_file in self.active_pid_files():
            result = force_kill_from_pid_file(pid_file)
            if result.server_gone or (final and result.status == "no_pid_file"):
                with self._lock:
                    self._pid_files.discard(pid_file)
            else:
                all_gone = False
        return all_gone

    def _cancel_tasks(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    # --- 提交工作 -----------------------------------------------------------

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        with self._lock:
            if self._closed or self._loop is None:
                coro.close()
                raise RuntimeError("worker 未啟動或已關閉")
            job = _Job(coro)
            self._active_jobs += 1
            future = asyncio.run_coroutine_threadsafe(self._tracked(job), self._loop)
        future.add_done_callback(functools.partial(self._on_future_done, job))
        return future

    def call_soon(self, callback: Callable[..., None], *args) -> None:
        """在 worker loop 內執行 callback（例如解決等待中的確認 Future）。"""
        if self._loop is None:
            raise RuntimeError("worker 未啟動")
        self._loop.call_soon_threadsafe(callback, *args)

    async def _tracked(self, job: _Job):
        with self._lock:
            if job.finished:  # 對外 Future 在開始前就被取消
                raise asyncio.CancelledError
            job.started = True
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            return await job.coroutine
        finally:
            self._tasks.discard(task)
            self._finish_job(job)

    def _on_future_done(self, job: _Job, future: concurrent.futures.Future) -> None:
        # 對外 Future 被取消不代表 loop 內的工作結束；只有「還沒開始就被取消」才在這裡結算。
        # 檢查 started 與標記 finished 必須在同一個臨界區，否則 _tracked 可能剛好在中間開始
        if not future.cancelled():
            return
        with self._jobs_changed:
            if job.started or job.finished:
                return
            self._mark_finished_locked(job)
        job.coroutine.close()

    def _finish_job(self, job: _Job) -> None:
        with self._jobs_changed:
            if not job.finished:
                self._mark_finished_locked(job)

    def _mark_finished_locked(self, job: _Job) -> None:
        job.finished = True
        self._active_jobs -= 1
        self._jobs_changed.notify_all()

    # --- MCP 每輪連線 -------------------------------------------------------

    def active_pid_files(self) -> list[Path]:
        with self._lock:
            return list(self._pid_files)

    async def run_with_mcp(self, body: McpBody[T]) -> T:
        """開一條新的 MCP stdio 連線執行 body(session, tools)，結束（含例外、取消）一定關閉連線。"""
        pid_file = Path(tempfile.gettempdir()) / f"{PID_FILE_PREFIX}{uuid.uuid4().hex}.pid"
        with self._lock:
            self._pid_files.add(pid_file)
        stack = AsyncExitStack()
        try:
            session, tools = await self._open_with_deadline(stack, pid_file)
            return await body(TimeoutSession(session, self.config.tool_timeout), tools)
        finally:
            try:
                await self._close_with_deadline(stack, pid_file)
            finally:
                # 強制終止失敗時 PID 檔保留，留給 stop() 最後再清一次
                if not pid_file.exists():
                    with self._lock:
                        self._pid_files.discard(pid_file)

    async def _open_with_deadline(self, stack: AsyncExitStack, pid_file: Path):
        config = self.config
        loop = asyncio.get_running_loop()
        # asyncio.timeout 只是送出取消；啟動卡在不理會取消的地方時，由計時器強制終止 Server 讓它解開
        kill_timer = ForceKillTimer(loop, pid_file, config.startup_timeout + config.cancel_grace)
        try:
            async with asyncio.timeout(config.startup_timeout):
                return await self._open_session(stack, pid_file, config)
        finally:
            kill_timer.cancel()

    async def _close_with_deadline(self, stack: AsyncExitStack, pid_file: Path) -> None:
        config = self.config
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        # 記下進入時已有的取消（例如 body 被外部取消），只收回本函式自己發出的截止取消
        base_cancelling = task.cancelling()
        own_cancels = 0

        def cancel_cleanup() -> None:
            nonlocal own_cancels
            own_cancels += 1
            task.cancel()

        def withdraw_own_cancels() -> None:
            nonlocal own_cancels
            for _ in range(own_cancels):
                task.uncancel()
            own_cancels = 0

        cancel_handle = loop.call_later(config.shutdown_grace, cancel_cleanup)
        kill_timer = ForceKillTimer(loop, pid_file, config.shutdown_grace + config.cancel_grace)
        completed = False
        try:
            await stack.aclose()
            completed = True
        except asyncio.CancelledError:
            deadline_hit = own_cancels > 0
            withdraw_own_cancels()
            if not deadline_hit or task.cancelling() > base_cancelling:
                raise
            logger.warning("關閉 MCP 連線超過 %.1f 秒，已中斷 cleanup", config.shutdown_grace)
        finally:
            cancel_handle.cancel()
            kill_timer.cancel()
            if completed:
                # cleanup 正常跑完，MCP transport 已結束 Server
                with _pid_file_lock:
                    remove_pid_file(pid_file)
            else:
                # cleanup 被中斷或出錯，Server 可能還活著；PID 檔可能還沒寫出，等到 cancel_grace。
                # 阻塞的查詢與強殺放執行緒，輪詢用 asyncio.sleep，不卡住 loop
                result = await asyncio.to_thread(force_kill_from_pid_file, pid_file)
                deadline = loop.time() + config.cancel_grace
                while result.status == "no_pid_file" and loop.time() < deadline:
                    await asyncio.sleep(0.05)
                    result = await asyncio.to_thread(force_kill_from_pid_file, pid_file)
        if completed:
            # cleanup 吞掉了截止取消後才正常結束：收回自己的取消；若期間另有外部取消，照樣往上拋
            withdraw_own_cancels()
            if task.cancelling() > base_cancelling:
                raise asyncio.CancelledError

    async def check_ready(self) -> list[str]:
        """啟動就緒檢查：能開 stdio、initialize、list_tools 就回傳給 LLM 的工具名。"""

        async def names(_session, tools: list[dict]) -> list[str]:
            return [tool["name"] for tool in tools]

        return await self.run_with_mcp(names)
