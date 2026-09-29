"""HRMS 一句話請假 Agent 的 Gradio demo（任務 #6 里程碑 3）。

用法：python gradio_app.py [--employee E001] [--reset-db] [--port 7860] [--no-browser]

- 畫面邏輯分兩層：DemoHandlers 只產生 UiModel（純資料，可不開瀏覽器測），to_updates 才轉成 Gradio 元件更新。
- 所有會改狀態的事件共用同一個 concurrency_id、limit=1，並帶隱藏的 view_version；
  真正的入場控制在 DemoController（排隊不等於拒絕）。
- handler 收到「要確認／有結果／錯誤」任一結果就結束，不佔住唯一的 slot，確認按鈕才排得進來。
- 確認內容放在聊天區外的唯讀文字框；只綁 127.0.0.1、不開 share。

設計見 docs/tasks/06-hrms-gradio-demo.md §5–§9。
"""

import argparse
import logging
import os
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import gradio as gr

import leave_service
from agent_app import DEFAULT_MODEL, PROJECT_DIR, WEEKDAY_NAMES
from demo_controller import EMPLOYEES, DemoController, State, ViewState
from demo_views import Table, load_views
from demo_worker import AgentWorker, WorkerConfig

logger = logging.getLogger(__name__)

# 與終端機版手動驗收一致：固定「今天」，「下週三」＝2026-09-16
DEMO_TODAY = date(2026, 9, 13)
CONCURRENCY_ID = "hrms-demo"
SERVER_NAME = "127.0.0.1"
INIT_SQL = PROJECT_DIR / "init_db.sql"

STATUS_LABELS = {
    State.IDLE: "🟢 可以輸入請假需求",
    State.BUSY: "⏳ 處理中…",
    State.AWAIT_CONFIRM: "📝 請確認下方假單內容",
    State.CLOSING: "⛔ demo 已關閉",
    State.FATAL: "⛔ 發生無法恢復的錯誤，請修正設定後重新啟動",
}
NO_PENDING_CONFIRM = "（目前沒有待確認的假單）"
# 瀏覽器自動翻譯會把中文再翻一次（「時段」變「贏得」），而且會替換文字節點，
# 之後 Gradio 更新的是舊節點，畫面就停在舊數字；所以整頁標成繁中且禁止翻譯
NO_TRANSLATE_HEAD = '<meta name="google" content="notranslate">'
NO_TRANSLATE_JS = """() => {
    const root = document.documentElement;
    root.lang = "zh-Hant";
    root.setAttribute("translate", "no");
    root.classList.add("notranslate");
}"""


@dataclass(frozen=True)
class UiModel:
    version: int
    header: str
    status: str
    chat: list[dict]
    input_enabled: bool
    clear_input: bool
    confirm_text: str
    confirm_enabled: bool
    employee: str
    employee_enabled: bool
    calendar: list[Table]
    balance: Table


def safe_model_name(model: str) -> str:
    # 模型名來自環境變數，放進 Markdown 前只留常見字元
    return re.sub(r"[^A-Za-z0-9._:-]", "", model) or "unknown"


class DemoHandlers:
    """Gradio 事件處理：呼叫 controller，產生 UiModel。不直接碰 worker 或 asyncio。"""

    def __init__(self, controller: DemoController, db_path: str | Path, *, today: date, model: str):
        self.controller = controller
        self.db_path = db_path
        self.today = today
        self.model = model

    def render(self, view: ViewState, *, clear_input: bool = False) -> UiModel:
        calendar, balance = load_views(self.db_path, view.employee_id, self.today)
        weekday = WEEKDAY_NAMES[self.today.weekday()]
        header = (
            f"### HRMS 一句話請假 Demo\n"
            f"員工 **{view.employee_id}**｜今天 **{self.today.isoformat()}（{weekday}）**｜模型 `{safe_model_name(self.model)}`"
        )
        status = STATUS_LABELS[view.state]
        if view.notice:
            status += f"　{view.notice}"
        awaiting = view.state == State.AWAIT_CONFIRM
        return UiModel(
            version=view.version,
            header=header,
            status=status,
            chat=[{"role": role, "content": text} for role, text in view.chat],
            input_enabled=view.state == State.IDLE,
            clear_input=clear_input,
            confirm_text=view.confirm_text if awaiting and view.confirm_text else NO_PENDING_CONFIRM,
            confirm_enabled=awaiting,
            employee=view.employee_id,
            employee_enabled=view.state == State.IDLE,
            calendar=calendar,
            balance=balance,
        )

    def on_load(self) -> Iterator[UiModel]:
        """重新整理頁面時同步目前狀態；若正在處理，接手等到結果（原本的 handler 可能已隨舊頁面消失）。"""
        view = self.controller.view()
        yield self.render(view)
        if not view.settled:
            yield self.render(self.controller.wait_until_settled(view.version))

    def on_send(self, version, text) -> Iterator[UiModel]:
        outcome = self.controller.send(to_version(version), text)
        yield self.render(outcome.view, clear_input=outcome.accepted)
        if outcome.accepted and not outcome.view.settled:
            yield self.render(self.controller.wait_until_settled(outcome.view.version))

    def on_answer(self, version, approved: bool) -> Iterator[UiModel]:
        outcome = self.controller.answer(to_version(version), approved)
        yield self.render(outcome.view)
        if outcome.accepted and not outcome.view.settled:
            yield self.render(self.controller.wait_until_settled(outcome.view.version))

    def on_switch(self, version, employee_id) -> Iterator[UiModel]:
        # 被拒時也回傳目前畫面：下拉選單會被設回目前員工
        yield self.render(self.controller.switch_employee(to_version(version), employee_id).view)


def to_version(value) -> int:
    """隱藏 Number 元件傳回的版本號；無法解析時回 -1（一定被拒）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


# ---------------------------------------------------------------------------
# Gradio
# ---------------------------------------------------------------------------


def table_update(table: Table):
    return gr.update(value={"headers": table.headers, "data": table.rows}, label=table.title)


def to_updates(model: UiModel) -> tuple:
    """UiModel → 依 build_app 的 outputs 順序排列的元件更新。"""
    this_week, next_week = model.calendar
    return (
        model.version,
        model.header,
        model.status,
        model.chat,
        gr.update(value="", interactive=model.input_enabled) if model.clear_input else gr.update(interactive=model.input_enabled),
        gr.update(interactive=model.input_enabled),
        model.confirm_text,
        gr.update(interactive=model.confirm_enabled),
        gr.update(interactive=model.confirm_enabled),
        gr.update(value=model.employee, interactive=model.employee_enabled),
        table_update(this_week),
        table_update(next_week),
        table_update(model.balance),
    )


def _stream(models: Iterator[UiModel]) -> Iterator[tuple]:
    for model in models:
        yield to_updates(model)


def build_app(handlers: DemoHandlers) -> gr.Blocks:
    controller = handlers.controller
    # 本機 demo 不需要把使用統計送到 Gradio／Hugging Face
    with gr.Blocks(title="HRMS 一句話請假 Demo", analytics_enabled=False) as demo:
        # 入場控制用：前端在觸發當下就把值送出，排隊的事件帶的是舊版本（gr.State 存在伺服器端，不能用）
        version = gr.Number(value=0, precision=0, visible=False)
        header = gr.Markdown()
        with gr.Row():
            with gr.Column(scale=5):
                chatbot = gr.Chatbot(
                    label="對話",
                    height=440,
                    sanitize_html=True,
                    allow_tags=False,
                    render_markdown=True,
                    feedback_options=None,
                )
                with gr.Row():
                    message = gr.Textbox(
                        placeholder="例如：下週三下午請特休",
                        show_label=False,
                        max_length=500,
                        scale=5,
                    )
                    send_button = gr.Button("送出", variant="primary", scale=1)
                status = gr.Markdown()
                confirm_box = gr.Textbox(
                    label="📝 請確認假單內容（系統依實際參數試算，不是 AI 產生的文字）",
                    value=NO_PENDING_CONFIRM,
                    lines=7,
                    interactive=False,
                )
                with gr.Row():
                    approve_button = gr.Button("✅ 確認送出", variant="primary", interactive=False)
                    reject_button = gr.Button("❌ 取消", variant="stop", interactive=False)
            with gr.Column(scale=4):
                employee = gr.Dropdown(
                    choices=list(controller.employees),
                    value=controller.view().employee_id,
                    label="員工（切換會重新開始對話）",
                )
                this_week = gr.Dataframe(type="array", datatype="str", interactive=False, label="本週")
                next_week = gr.Dataframe(type="array", datatype="str", interactive=False, label="下週")
                balance = gr.Dataframe(type="array", datatype="str", interactive=False, label="額度（小時）")

        outputs = [
            version,
            header,
            status,
            chatbot,
            message,
            send_button,
            confirm_box,
            approve_button,
            reject_button,
            employee,
            this_week,
            next_week,
            balance,
        ]
        guarded = dict(
            outputs=outputs,
            concurrency_id=CONCURRENCY_ID,
            concurrency_limit=1,
            api_visibility="private",
            show_progress="hidden",
        )

        def send(version_value, text):
            yield from _stream(handlers.on_send(version_value, text))

        def approve(version_value):
            yield from _stream(handlers.on_answer(version_value, True))

        def reject(version_value):
            yield from _stream(handlers.on_answer(version_value, False))

        def switch(version_value, employee_id):
            yield from _stream(handlers.on_switch(version_value, employee_id))

        def load():
            yield from _stream(handlers.on_load())

        send_button.click(send, inputs=[version, message], **guarded)
        message.submit(send, inputs=[version, message], **guarded)
        approve_button.click(approve, inputs=[version], **guarded)
        reject_button.click(reject, inputs=[version], **guarded)
        # .input 只在使用者操作時觸發；程式把下拉選單設回目前員工時不會再觸發
        employee.input(switch, inputs=[version, employee], **guarded)
        # 載入頁面只讀狀態，不與上面搶 slot（BUSY 時才不會卡住重新整理）
        demo.load(load, outputs=outputs, concurrency_limit=None, api_visibility="private", show_progress="hidden")
    return demo


# ---------------------------------------------------------------------------
# 啟動
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HRMS 一句話請假 Gradio demo（只在本機 127.0.0.1）")
    parser.add_argument("--employee", choices=EMPLOYEES, default=EMPLOYEES[0], help="一開始的員工")
    parser.add_argument("--reset-db", action="store_true", help="啟動前用 init_db.sql 重建資料庫（預演用）")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--no-browser", action="store_true", help="不要自動開瀏覽器")
    return parser.parse_args(argv)


def reset_database(db_path: str | Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(INIT_SQL.read_text(encoding="utf-8"))
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    import anthropic
    from dotenv import load_dotenv

    load_dotenv(PROJECT_DIR / ".env")
    model = os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    db_path = Path(os.environ.get("HRMS_DB_PATH") or leave_service.DEFAULT_DB_PATH)
    if args.reset_db:
        reset_database(db_path)
        print(f"已重建資料庫：{db_path}")

    config = WorkerConfig(db_path=db_path)
    worker = AgentWorker(config)
    worker.start()
    try:
        ready_timeout = config.startup_timeout + config.shutdown_grace + config.cancel_grace + 5
        tools = worker.submit(worker.check_ready()).result(timeout=ready_timeout)
    except Exception as exc:  # noqa: BLE001 - 啟動檢查失敗就不開 UI，把原因印出來
        print(f"MCP Server 啟動檢查失敗，不啟動 demo：{exc!r}")
        worker.stop()
        return 1
    print(f"MCP Server 就緒，工具：{', '.join(tools)}")

    controller = DemoController(worker, anthropic.AsyncAnthropic(), today=DEMO_TODAY, employee_id=args.employee, model=model)
    demo = build_app(DemoHandlers(controller, db_path, today=DEMO_TODAY, model=model))
    try:
        # 只綁本機：公開分享等於讓拿到連結的人用你的 API key 以任意員工請假
        demo.queue(default_concurrency_limit=1).launch(
            server_name=SERVER_NAME,
            server_port=args.port,
            share=False,
            inbrowser=not args.no_browser,
            footer_links=[],  # 不顯示「透過 API 使用」等連結
            head=NO_TRANSLATE_HEAD,
            js=NO_TRANSLATE_JS,
        )
    except KeyboardInterrupt:
        pass
    finally:
        if not controller.close():
            print("警告：背景程式或 MCP Server 沒有完全關閉，請檢查是否有殘留的 python 行程。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
