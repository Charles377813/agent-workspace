"""Gradio demo 的 controller（任務 #6 里程碑 2）：狀態機、view_version 入場控制、錯誤分類與回滾。

- 所有操作在同一把鎖下「檢查版本與狀態 → 轉移」；Gradio 共用 concurrency_limit 只是排隊，不是拒絕，
  BUSY 時排隊的點擊帶的是舊版本，輪到時一律被拒。
- 狀態由 worker 在事件發生當下直接轉移（不靠 UI handler 取事件），handler 只等「版本前進且不是 BUSY」。
- 確認的 Future 只在 worker loop 內建立、檢查與解決。
- 錯誤時把 Anthropic 對話歷史回滾到本輪開始前，避免下一輪帶著半套 tool_use 送出。

設計見 docs/tasks/06-hrms-gradio-demo.md §3、§4。
"""

import asyncio
import enum
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from agent_app import AUTH_RESOLVE_ERROR, DEFAULT_MODEL, build_system_prompt, describe_api_error, run_turn
from demo_worker import AgentWorker

logger = logging.getLogger(__name__)

EMPLOYEES = ("E001", "E002", "E003")
MAX_INPUT_CHARS = 500

RECOVERABLE_REPLY = "處理失敗，請再試一次。"
TIMEOUT_REPLY = "連線逾時，請再試一次。"
RESET_REPLY = "請求無效，對話已重置，請重新描述請假需求。"
CANCELLED_REPLY = "這次處理已中止。"


class State(enum.StrEnum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    AWAIT_CONFIRM = "AWAIT_CONFIRM"
    CLOSING = "CLOSING"
    FATAL = "FATAL"


# BUSY 以外都是 handler 可以結束等待的狀態
SETTLED_STATES = frozenset({State.IDLE, State.AWAIT_CONFIRM, State.CLOSING, State.FATAL})


class ErrorKind(enum.StrEnum):
    FATAL = "fatal"  # 認證、模型不存在：重試也不會好
    RESET = "reset"  # 400：清空對話歷史後可繼續
    RECOVERABLE = "recoverable"  # 其他：回滾本輪後可再試


def classify_turn_error(exc: BaseException) -> tuple[ErrorKind, str]:
    """demo 專用錯誤分類；和 CLI 不同，400 不結束程式而是清空對話。"""
    import anthropic

    if isinstance(exc, TypeError) and AUTH_RESOLVE_ERROR in str(exc):
        return ErrorKind.FATAL, describe_api_error(exc)[0]
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.NotFoundError)):
        return ErrorKind.FATAL, describe_api_error(exc)[0]
    if isinstance(exc, anthropic.BadRequestError):
        return ErrorKind.RESET, RESET_REPLY
    if isinstance(exc, anthropic.APIError):
        return ErrorKind.RECOVERABLE, describe_api_error(exc)[0]
    if isinstance(exc, TimeoutError):
        return ErrorKind.RECOVERABLE, TIMEOUT_REPLY
    return ErrorKind.RECOVERABLE, RECOVERABLE_REPLY


@dataclass(frozen=True)
class ViewState:
    state: State
    version: int
    employee_id: str
    chat: tuple[tuple[str, str], ...]  # (role, text)，role 為 "user"／"assistant"
    confirm_text: str
    notice: str

    @property
    def settled(self) -> bool:
        return self.state in SETTLED_STATES


@dataclass(frozen=True)
class Outcome:
    accepted: bool
    view: ViewState


class DemoController:
    """單人本機 demo 的對話狀態。UI handler 只呼叫這裡的同步方法，不直接碰 worker 或 asyncio。"""

    def __init__(
        self,
        worker: AgentWorker,
        client,
        *,
        today: date,
        employee_id: str = EMPLOYEES[0],
        model: str = DEFAULT_MODEL,
        employees: tuple[str, ...] = EMPLOYEES,
        system_prompt: Callable[[date, str], str] = build_system_prompt,
    ):
        if employee_id not in employees:
            raise ValueError(f"未知的員工：{employee_id}")
        self._worker = worker
        self._client = client
        self._today = today
        self._model = model
        self._employees = employees
        self._system_prompt = system_prompt

        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._state = State.IDLE
        self._version = 0
        self._employee_id = employee_id
        self._chat: list[tuple[str, str]] = []
        self._confirm_text = ""
        self._notice = ""
        self._turn_id = 0
        # Anthropic 對話歷史：只在 worker loop 內被 run_turn 修改；UI 執行緒只在 IDLE 時清空
        self._messages: list = []
        # (turn_id, Future)：只在 worker loop 內讀寫 Future 本身
        self._pending: tuple[int, asyncio.Future] | None = None

    # --- 讀取 ---------------------------------------------------------------

    @property
    def employees(self) -> tuple[str, ...]:
        return self._employees

    @property
    def today(self) -> date:
        return self._today

    @property
    def model(self) -> str:
        return self._model

    def view(self) -> ViewState:
        with self._lock:
            return self._view_locked()

    def _view_locked(self) -> ViewState:
        return ViewState(
            state=self._state,
            version=self._version,
            employee_id=self._employee_id,
            chat=tuple(self._chat),
            confirm_text=self._confirm_text,
            notice=self._notice,
        )

    def _bump_locked(self, state: State) -> None:
        self._state = state
        self._version += 1
        self._changed.notify_all()

    def wait_until_settled(self, seen_version: int, timeout: float | None = None) -> ViewState:
        """等到版本超過 seen_version 且不是 BUSY（有結果、要確認、關閉或致命錯誤）。

        不設 UI 逾時：worker 保證每一輪都會以回覆或錯誤結束。timeout 只給測試用。
        """
        with self._changed:
            self._changed.wait_for(lambda: self._version > seen_version and self._state in SETTLED_STATES, timeout)
            return self._view_locked()

    # --- UI 操作（入場控制） ------------------------------------------------

    def send(self, version: int, text: str) -> Outcome:
        text = (text or "").strip()
        with self._lock:
            if version != self._version or self._state != State.IDLE or not text:
                return Outcome(False, self._view_locked())
            if len(text) > MAX_INPUT_CHARS:
                self._notice = f"訊息太長（上限 {MAX_INPUT_CHARS} 字）。"
                self._bump_locked(State.IDLE)
                return Outcome(False, self._view_locked())
            if not self._worker.is_alive:
                self._notice = "背景程式已停止，請重新啟動 demo。"
                self._bump_locked(State.FATAL)
                return Outcome(False, self._view_locked())

            self._turn_id += 1
            turn_id = self._turn_id
            employee_id = self._employee_id
            self._chat.append(("user", text))
            self._confirm_text = ""
            self._notice = ""
            self._bump_locked(State.BUSY)
            try:
                self._worker.submit(self._run(turn_id, employee_id, text))
            except RuntimeError:
                self._chat.pop()
                self._notice = "背景程式已停止，請重新啟動 demo。"
                self._bump_locked(State.FATAL)
                return Outcome(False, self._view_locked())
            return Outcome(True, self._view_locked())

    def answer(self, version: int, approved: bool) -> Outcome:
        with self._lock:
            if version != self._version or self._state != State.AWAIT_CONFIRM or self._pending is None:
                return Outcome(False, self._view_locked())
            turn_id = self._pending[0]
            # 先排程成功才轉移狀態：loop 已關閉時 call_soon 會拋 RuntimeError，不能留在 BUSY 卡住。
            # 檢查 Future 是否仍待決＋set_result 整段在 worker loop 內做；
            # confirm 的 finally 要持鎖清 _pending，所以在這裡持鎖排程不會讓 loop 搶先改狀態
            try:
                self._worker.call_soon(self._resolve_pending, turn_id, approved is True)
            except RuntimeError:
                self._confirm_text = ""
                self._notice = "背景程式已停止，請重新啟動 demo。"
                self._bump_locked(State.FATAL)
                return Outcome(False, self._view_locked())
            self._chat.append(("user", "✅ 確認送出" if approved else "❌ 取消"))
            self._confirm_text = ""
            self._bump_locked(State.BUSY)
            return Outcome(True, self._view_locked())

    def switch_employee(self, version: int, employee_id: str) -> Outcome:
        with self._lock:
            if (
                version != self._version
                or self._state != State.IDLE
                or employee_id not in self._employees
            ):
                return Outcome(False, self._view_locked())
            if employee_id != self._employee_id:
                self._employee_id = employee_id
                self._messages.clear()
                self._chat.clear()
                self._notice = f"已切換為員工 {employee_id}，對話已重新開始。"
            self._bump_locked(State.IDLE)
            return Outcome(True, self._view_locked())

    def close(self) -> bool:
        """關閉 demo：之後所有操作被拒；等待中的確認以取消解決，worker 有序關閉。回傳是否乾淨。"""
        with self._lock:
            if self._state == State.CLOSING:
                return not self._worker.is_alive
            self._notice = "demo 已關閉。"
            self._bump_locked(State.CLOSING)
        return self._worker.stop(before_stop=self._reject_any_pending)

    # --- worker loop 內 -----------------------------------------------------

    def _resolve_pending(self, turn_id: int, approved: bool) -> None:
        pending = self._pending
        if pending is None or pending[0] != turn_id or pending[1].done():
            return
        pending[1].set_result(approved)

    def _reject_any_pending(self) -> None:
        pending = self._pending
        if pending is not None and not pending[1].done():
            pending[1].set_result(False)

    def _make_confirm(self, turn_id: int):
        async def confirm(prompt: str) -> bool:
            future = asyncio.get_running_loop().create_future()
            with self._lock:
                if self._turn_id != turn_id or self._state != State.BUSY:
                    return False
                self._pending = (turn_id, future)
                self._confirm_text = prompt
                self._bump_locked(State.AWAIT_CONFIRM)
            try:
                return (await future) is True
            finally:
                # answer() 在 UI 執行緒持鎖讀 _pending，清除也要持鎖
                with self._lock:
                    if self._pending is not None and self._pending[1] is future:
                        self._pending = None

        return confirm

    async def _run(self, turn_id: int, employee_id: str, text: str) -> None:
        checkpoint = len(self._messages)

        async def body(session, tools: list[dict]) -> str:
            return await run_turn(
                self._client,
                session,
                self._messages,
                text,
                system=self._system_prompt(self._today, employee_id),
                tools=tools,
                allowed_tools=frozenset(tool["name"] for tool in tools),
                employee_id=employee_id,
                model=self._model,
                confirm=self._make_confirm(turn_id),
            )

        try:
            reply = await self._worker.run_with_mcp(body)
        except asyncio.CancelledError:
            self._finish_error(turn_id, checkpoint, ErrorKind.RECOVERABLE, CANCELLED_REPLY)
            raise
        except Exception as exc:  # noqa: BLE001 - 每一輪都必須以回覆或錯誤結束，不能讓 handler 永遠等
            kind, message = classify_turn_error(exc)
            if kind == ErrorKind.RECOVERABLE and not _is_known_transient(exc):
                logger.exception("第 %d 輪處理失敗", turn_id)
            self._finish_error(turn_id, checkpoint, kind, message)
        else:
            self._finish_reply(turn_id, reply)
        finally:
            self._reject_any_pending()

    def _finish_reply(self, turn_id: int, reply: str) -> None:
        with self._lock:
            if turn_id != self._turn_id or self._state in {State.CLOSING, State.FATAL}:
                return
            self._chat.append(("assistant", reply))
            self._confirm_text = ""
            self._bump_locked(State.IDLE)

    def _finish_error(self, turn_id: int, checkpoint: int, kind: ErrorKind, message: str) -> None:
        with self._lock:
            # 先恢復對話歷史 invariant，才能回 IDLE
            if kind == ErrorKind.RESET:
                self._messages.clear()
            else:
                del self._messages[checkpoint:]
            if turn_id != self._turn_id or self._state == State.CLOSING:
                return
            self._confirm_text = ""
            if kind == ErrorKind.RESET:
                self._chat.clear()
            self._chat.append(("assistant", f"⚠️ {message}"))
            self._notice = message
            self._bump_locked(State.FATAL if kind == ErrorKind.FATAL else State.IDLE)


def _is_known_transient(exc: BaseException) -> bool:
    import anthropic

    return isinstance(exc, (TimeoutError, anthropic.APIError))
