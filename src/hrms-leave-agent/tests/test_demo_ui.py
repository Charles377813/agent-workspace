"""Gradio demo 畫面層（任務 #6 里程碑 3）：UiModel、handler 產生的畫面序列、元件安全設定、啟動參數。

不開瀏覽器；handler 用真的 DemoController＋假 worker session＋假 Claude client。
"""

import threading
from datetime import date

import gradio as gr
import pytest

import gradio_app
from demo_controller import DemoController
from demo_worker import AgentWorker, WorkerConfig
from gradio_app import CONCURRENCY_ID, NO_PENDING_CONFIRM, DemoHandlers, build_app, to_updates, to_version
from test_agent_loop import reply, text_block
from test_demo_controller import Gate, ScriptedClient, apply_client, make_controller  # noqa: F401 - fixture

TODAY = date(2026, 9, 13)
WAIT = 10


@pytest.fixture
def handlers_for(make_controller, db_path):
    def make(client, **kwargs):
        controller = make_controller(client, **kwargs)
        return DemoHandlers(controller, db_path, today=TODAY, model="claude-opus-5")

    return make


def test_idle_model_enables_input_and_disables_confirm(handlers_for):
    handlers = handlers_for(ScriptedClient())
    model = handlers.render(handlers.controller.view())
    assert model.input_enabled and model.employee_enabled
    assert not model.confirm_enabled and model.confirm_text == NO_PENDING_CONFIRM
    assert "E001" in model.header and "2026-09-13（日）" in model.header and "claude-opus-5" in model.header
    assert model.balance.rows[0] == ["特休", "80", "24", "56"]
    assert [table.title for table in model.calendar] == ["本週", "下週"]


def test_send_yields_busy_then_confirm(handlers_for):
    handlers = handlers_for(apply_client())
    busy, awaiting = list(handlers.on_send(0, "下週三下午請特休"))

    assert busy.clear_input and not busy.input_enabled and not busy.confirm_enabled and not busy.employee_enabled
    assert busy.chat == [{"role": "user", "content": "下週三下午請特休"}]

    assert awaiting.status.startswith("📝")
    assert awaiting.confirm_enabled and not awaiting.input_enabled and not awaiting.employee_enabled
    assert "56 → 52" in awaiting.confirm_text
    # 確認內容不在聊天區
    assert all("56 → 52" not in message["content"] for message in awaiting.chat)


def test_confirm_then_reply_refreshes_calendar_and_balance_from_real_db(db_path):
    """真 MCP stdio＋SQLite：按下確認後，最終畫面的週曆出現特休、額度 56 → 52（驗證寫入提交後才重讀）。"""
    worker = AgentWorker(WorkerConfig(startup_timeout=20, tool_timeout=20, shutdown_grace=5, cancel_grace=2, db_path=db_path))
    worker.start()
    controller = DemoController(worker, apply_client("已送出"), today=TODAY)
    handlers = DemoHandlers(controller, db_path, today=TODAY, model="claude-sonnet-5")
    try:
        *_, awaiting = handlers.on_send(0, "下週三下午請特休")
        assert awaiting.confirm_enabled
        before_week = awaiting.calendar[1]
        assert before_week.rows[1][3] == ""  # 下週／下午／9/16 三
        assert awaiting.balance.rows[0] == ["特休", "80", "24", "56"]

        busy, done = list(handlers.on_answer(awaiting.version, True))
        assert not busy.confirm_enabled
        next_week = done.calendar[1]
        assert next_week.headers[3] == "9/16 三"
        assert next_week.rows[1] == ["下午", "", "", "特休", "", ""]
        assert next_week.rows[0][3] == ""
        assert done.balance.rows[0] == ["特休", "80", "28", "52"]
        assert done.chat[-1] == {"role": "assistant", "content": "已送出"}
    finally:
        assert controller.close() is True


def test_rejected_send_does_not_clear_input_and_stops(handlers_for):
    handlers = handlers_for(ScriptedClient())
    models = list(handlers.on_send(99, "排隊的舊點擊"))
    assert len(models) == 1
    assert not models[0].clear_input and models[0].input_enabled


def test_rejected_answer_yields_single_current_view(handlers_for):
    handlers = handlers_for(apply_client())
    *_, awaiting = handlers.on_send(0, "下週三下午請特休")
    models = list(handlers.on_answer(awaiting.version - 1, True))
    assert len(models) == 1 and models[0].confirm_enabled


def test_switch_rejected_resets_dropdown_to_current_employee(handlers_for):
    handlers = handlers_for(apply_client())
    *_, awaiting = handlers.on_send(0, "下週三下午請特休")
    [model] = handlers.on_switch(awaiting.version, "E002")
    assert model.employee == "E001" and not model.employee_enabled


def test_switch_accepted_updates_balance(handlers_for):
    handlers = handlers_for(ScriptedClient())
    [model] = handlers.on_switch(0, "E002")
    assert model.employee == "E002" and model.balance.rows[0] == ["特休", "56", "56", "0"]


def test_load_during_busy_waits_for_result(handlers_for):
    gate = Gate(reply("end_turn", text_block("好")))
    handlers = handlers_for(ScriptedClient(gate))
    assert handlers.controller.send(0, "你好").accepted
    assert gate.entered.wait(WAIT)

    results = []
    loader = threading.Thread(target=lambda: results.extend(handlers.on_load()))
    loader.start()
    loader.join(0.3)
    assert loader.is_alive()  # BUSY：重新整理後接手等待
    gate.release.set()
    loader.join(WAIT)
    assert [model.input_enabled for model in results] == [False, True]


def test_load_when_idle_yields_once(handlers_for):
    handlers = handlers_for(ScriptedClient())
    assert len(list(handlers.on_load())) == 1


@pytest.mark.parametrize("value, expected", [(3, 3), (3.0, 3), ("4", 4), (None, -1), ("abc", -1)])
def test_to_version(value, expected):
    assert to_version(value) == expected


def test_to_updates_order_and_input_clearing(handlers_for):
    handlers = handlers_for(ScriptedClient())
    model = handlers.render(handlers.controller.view(), clear_input=True)
    updates = to_updates(model)
    assert len(updates) == 13
    assert updates[0] == model.version
    assert updates[4]["value"] == "" and updates[4]["interactive"] is True
    assert "value" not in to_updates(handlers.render(handlers.controller.view()))[4]
    assert updates[10]["value"]["headers"][1] == "9/7 一" and updates[10]["label"] == "本週"


def find_components(demo, cls):
    return [block for block in demo.blocks.values() if isinstance(block, cls)]


def test_build_app_component_safety_settings(handlers_for):
    handlers = handlers_for(ScriptedClient())
    demo = build_app(handlers)
    assert demo.analytics_enabled is False
    [chatbot] = find_components(demo, gr.Chatbot)
    assert chatbot.sanitize_html is True and chatbot.allow_tags is False

    textboxes = find_components(demo, gr.Textbox)
    [confirm_box] = [box for box in textboxes if "確認" in str(box.label)]
    assert confirm_box.interactive is False

    [version] = find_components(demo, gr.Number)
    assert version.visible is False
    for dataframe in find_components(demo, gr.Dataframe):
        assert dataframe.interactive is False and dataframe.datatype == "str"


def test_main_launches_on_localhost_without_share_and_closes(monkeypatch, db_path, tmp_path):
    captured = {}
    monkeypatch.setenv("HRMS_DB_PATH", str(db_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    # main() 會 load_dotenv，但它不覆蓋已存在的環境變數，上面兩個設定會保留

    def fake_launch(self, **kwargs):
        captured.update(kwargs)

    closed = []
    real_close = gradio_app.DemoController.close

    def tracking_close(self):
        result = real_close(self)
        closed.append(result)
        return result

    monkeypatch.setattr(gr.Blocks, "launch", fake_launch)
    monkeypatch.setattr(gradio_app.DemoController, "close", tracking_close)
    assert gradio_app.main(["--no-browser", "--port", "7999"]) == 0
    assert captured["server_name"] == "127.0.0.1"
    assert captured["share"] is False
    assert captured["server_port"] == 7999
    assert captured["footer_links"] == []
    assert 'content="notranslate"' in captured["head"]
    assert 'translate", "no"' in captured["js"] and "zh-Hant" in captured["js"]
    assert closed == [True]


def test_reset_db_restores_seed_data(db_path, db):
    db.execute("UPDATE leave_balances SET used_hours = 0 WHERE employee_id = 'E002'")
    gradio_app.reset_database(db_path)
    used = db.execute(
        "SELECT used_hours FROM leave_balances WHERE employee_id = 'E002' AND leave_type = 'ANNUAL'"
    ).fetchone()[0]
    assert used == 56


def test_safe_model_name_strips_markdown():
    assert gradio_app.safe_model_name("claude-opus-5") == "claude-opus-5"
    assert gradio_app.safe_model_name("x`](javascript:alert(1))") == "xjavascript:alert1"
    assert gradio_app.safe_model_name("`") == "unknown"


def test_state_changing_events_share_one_guarded_slot(handlers_for):
    """防止綁定參數被誤刪：改狀態的事件共用 concurrency_id、limit=1、private；load 不佔同一個 slot。

    Gradio 實際排隊與前端擷取 view_version 的行為，以瀏覽器手動 checklist 驗證。
    """
    demo = build_app(handlers_for(ScriptedClient()))
    functions = list(demo.fns.values())
    triggers = {
        (block_id_to_type(demo, target[0]), target[1]): fn
        for fn in functions
        for target in fn.targets
    }
    guarded = [
        triggers[("button", "click")],
        triggers[("textbox", "submit")],
        triggers[("dropdown", "input")],
    ]
    buttons = [fn for (kind, event), fn in triggers.items() if (kind, event) == ("button", "click")]
    guarded_fns = {id(fn) for fn in functions if fn.concurrency_id == CONCURRENCY_ID}
    assert len(guarded_fns) == 5  # send、Enter、確認、取消、切換員工
    for fn in functions:
        if fn.concurrency_id == CONCURRENCY_ID:
            assert fn.concurrency_limit == 1 and fn.api_visibility == "private"
    assert all(id(fn) in guarded_fns for fn in guarded)
    assert len(buttons) >= 1
    [load] = [fn for fn in functions if any(target[1] == "load" for target in fn.targets)]
    assert load.concurrency_id != CONCURRENCY_ID and load.concurrency_limit is None
    assert load.api_visibility == "private"


def block_id_to_type(demo, block_id):
    block = demo.blocks.get(block_id)
    return type(block).__name__.lower() if block is not None else "blocks"
