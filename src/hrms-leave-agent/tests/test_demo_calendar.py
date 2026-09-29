"""請假週曆與額度快照（任務 #6 里程碑 3）：純函式＋唯讀查詢。2026-09-13 是週日。"""

from datetime import date

import pytest

from demo_views import build_balance_table, build_leave_calendar, calendar_range, load_views, query_calendar_requests

SUNDAY = date(2026, 9, 13)


def marked_cells(tables):
    """{(週, 時段, 日期欄標題): 標籤}，只列有標記的格子。"""
    cells = {}
    for table in tables:
        for row in table.rows:
            for header, value in zip(table.headers[1:], row[1:]):
                if value:
                    cells[(table.title, row[0], header)] = value
    return cells


def test_week_starts_on_monday_before_sunday_today():
    this_week, next_week = build_leave_calendar([], SUNDAY)
    assert this_week.title == "本週" and next_week.title == "下週"
    assert this_week.headers == ["時段", "9/7 一", "9/8 二", "9/9 三", "9/10 四", "9/11 五"]
    assert next_week.headers[1] == "9/14 一" and next_week.headers[-1] == "9/18 五"
    assert [row[0] for row in this_week.rows] == ["上午", "下午"]
    assert marked_cells([this_week, next_week]) == {}


def test_week_start_when_today_is_monday():
    this_week, _ = build_leave_calendar([], date(2026, 9, 14))
    assert this_week.headers[1] == "9/14 一"


@pytest.mark.parametrize(
    "request_, expected",
    [
        # 下週三下午（demo 主情境）
        (("ANNUAL", "2026-09-16T14:00", "2026-09-16T18:00"), {("下週", "下午", "9/16 三"): "特休"}),
        # 整天：上午與下午都標
        (
            ("PERSONAL", "2026-09-15T09:00", "2026-09-15T18:00"),
            {("下週", "上午", "9/15 二"): "事假", ("下週", "下午", "9/15 二"): "事假"},
        ),
        # 部分時數也標整格
        (("SICK", "2026-09-17T10:00", "2026-09-17T12:00"), {("下週", "上午", "9/17 四"): "病假"}),
        # 午休 13–14 不標
        (("ANNUAL", "2026-09-17T13:00", "2026-09-17T14:00"), {}),
        # 相鄰邊界不標：上午結束在 13:00，不碰下午
        (("ANNUAL", "2026-09-18T09:00", "2026-09-18T13:00"), {("下週", "上午", "9/18 五"): "特休"}),
        # 跨天：週四下午到週五上午
        (
            ("ANNUAL", "2026-09-10T14:00", "2026-09-11T13:00"),
            {("本週", "下午", "9/10 四"): "特休", ("本週", "上午", "9/11 五"): "特休"},
        ),
        # 跨週末：本週五下午到下週一上午
        (
            ("ANNUAL", "2026-09-11T14:00", "2026-09-14T13:00"),
            {("本週", "下午", "9/11 五"): "特休", ("下週", "上午", "9/14 一"): "特休"},
        ),
        # 起點在範圍前、跨入本週一
        (("ANNUAL", "2026-09-04T14:00", "2026-09-07T13:00"), {("本週", "上午", "9/7 一"): "特休"}),
        # 終點跨出範圍：只標範圍內
        (("ANNUAL", "2026-09-18T14:00", "2026-09-21T18:00"), {("下週", "下午", "9/18 五"): "特休"}),
        # 完全在範圍外
        (("ANNUAL", "2026-09-21T09:00", "2026-09-21T18:00"), {}),
    ],
)
def test_calendar_marks_by_half_open_overlap(request_, expected):
    assert marked_cells(build_leave_calendar([request_], SUNDAY)) == expected


def test_same_cell_multiple_leaves_joined_without_duplicates():
    requests = [
        ("ANNUAL", "2026-09-16T14:00", "2026-09-16T15:00"),
        ("SICK", "2026-09-16T16:00", "2026-09-16T18:00"),
        ("ANNUAL", "2026-09-16T15:00", "2026-09-16T16:00"),
    ]
    assert marked_cells(build_leave_calendar(requests, SUNDAY)) == {("下週", "下午", "9/16 三"): "特休、病假"}


def test_unknown_type_and_html_are_plain_text():
    requests = [
        ("<img src=x onerror=alert(1)>", "2026-09-16T14:00", "2026-09-16T18:00"),
        ("BEREAVE‮", "2026-09-17T09:00", "2026-09-17T13:00"),
    ]
    cells = marked_cells(build_leave_calendar(requests, SUNDAY))
    assert cells[("下週", "下午", "9/16 三")] == "<img src=x onerror=alert(1)>"
    assert cells[("下週", "上午", "9/17 四")] == "BEREAVE\\u202e"  # 控制字元跳脫成可見文字


@pytest.mark.parametrize("start, end", [("bad", "2026-09-16T18:00"), ("2026-09-16T18:00", "2026-09-16T14:00"), (None, None)])
def test_malformed_times_are_skipped(start, end):
    assert marked_cells(build_leave_calendar([("ANNUAL", start, end)], SUNDAY)) == {}


def test_balance_table_orders_types_and_computes_remaining():
    table = build_balance_table([("SICK", 240, 8), ("ANNUAL", 80, 28), ("PERSONAL", 112, 0), ("<b>X</b>", 10, 3)])
    assert table.headers == ["假別", "總時數", "已用", "剩餘"]
    assert table.rows == [
        ["特休", "80", "28", "52"],
        ["事假", "112", "0", "112"],
        ["病假", "240", "8", "232"],
        ["<b>X</b>", "10", "3", "7"],
    ]


# ---------------------------------------------------------------------------
# 唯讀查詢
# ---------------------------------------------------------------------------


def insert_request(db, employee_id, start_at, end_at, status="SUBMITTED", leave_type="ANNUAL"):
    db.execute(
        "INSERT INTO leave_requests (employee_id, leave_type, start_at, end_at, hours, status) VALUES (?, ?, ?, ?, 4, ?)",
        (employee_id, leave_type, start_at, end_at, status),
    )


def test_calendar_range_is_two_weeks_from_monday():
    start, end = calendar_range(SUNDAY)
    assert start.isoformat() == "2026-09-07T00:00:00" and end.isoformat() == "2026-09-21T00:00:00"


def test_query_uses_overlap_status_and_employee(db_path, db):
    insert_request(db, "E001", "2026-09-16T14:00", "2026-09-16T18:00")
    insert_request(db, "E001", "2026-09-04T14:00", "2026-09-07T13:00")  # 起點在範圍前、跨入
    insert_request(db, "E001", "2026-09-18T14:00", "2026-09-22T18:00")  # 終點跨出
    insert_request(db, "E001", "2026-09-06T09:00", "2026-09-07T00:00")  # 剛好在範圍起點結束：不算
    insert_request(db, "E001", "2026-09-21T00:00", "2026-09-21T18:00")  # 剛好在範圍終點開始：不算
    insert_request(db, "E001", "2026-09-15T09:00", "2026-09-15T13:00", status="CANCELLED")
    insert_request(db, "E002", "2026-09-17T09:00", "2026-09-17T13:00")

    rows = query_calendar_requests(db_path, "E001", SUNDAY)
    assert rows == [
        ("ANNUAL", "2026-09-04T14:00", "2026-09-07T13:00"),
        ("ANNUAL", "2026-09-16T14:00", "2026-09-16T18:00"),
        ("ANNUAL", "2026-09-18T14:00", "2026-09-22T18:00"),
    ]


def test_load_views_reads_seed_balances(db_path):
    calendar, balance = load_views(db_path, "E001", SUNDAY)
    assert len(calendar) == 2
    assert balance.rows[0] == ["特休", "80", "24", "56"]
    _, e002 = load_views(db_path, "E002", SUNDAY)
    assert e002.rows[0] == ["特休", "56", "56", "0"]


def test_load_views_is_read_only_and_survives_missing_db(tmp_path):
    missing = tmp_path / "missing.db"
    calendar, balance = load_views(missing, "E001", SUNDAY)
    assert balance.rows == [] and len(calendar) == 2
    assert not missing.exists()  # 唯讀連線不會建出新檔
