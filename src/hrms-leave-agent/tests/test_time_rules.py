"""時間規則與時數計算。日期基準：2026-09-16 是週三。"""

import pytest

from leave_service import LeaveError, compute_hours, parse_time

WED = "2026-09-16"
FRI = "2026-09-18"
SAT = "2026-09-19"
SUN = "2026-09-20"
MON = "2026-09-21"


def assert_error(error_code, start_at, end_at):
    with pytest.raises(LeaveError) as exc:
        compute_hours(start_at, end_at)
    assert exc.value.error_code == error_code


# README「時數對照表」逐列
@pytest.mark.parametrize(
    "start_at, end_at, hours",
    [
        (f"{WED}T14:00", f"{WED}T18:00", 4),
        (f"{WED}T09:00", f"{WED}T18:00", 8),
        (f"{WED}T12:00", f"{WED}T15:00", 2),
        (f"{FRI}T17:00", f"{MON}T10:00", 2),
    ],
)
def test_readme_table_valid(start_at, end_at, hours):
    assert compute_hours(start_at, end_at) == hours


@pytest.mark.parametrize(
    "start_at, end_at, error_code",
    [
        (f"{WED}T13:00", f"{WED}T15:00", "INVALID_TIME_RANGE"),
        (f"{WED}T08:00", f"{WED}T18:00", "INVALID_TIME_RANGE"),
        (f"{SAT}T09:00", f"{MON}T18:00", "INVALID_TIME_RANGE"),
        (f"{WED}T14:30", f"{WED}T18:00", "INVALID_TIME_FORMAT"),
    ],
)
def test_readme_table_invalid(start_at, end_at, error_code):
    assert_error(error_code, start_at, end_at)


# 單日邊界
@pytest.mark.parametrize(
    "start_hour, end_hour, hours",
    [
        (9, 10, 1),
        (9, 13, 4),
        (12, 13, 1),
        (14, 15, 1),
        (17, 18, 1),
        (12, 18, 5),
        (9, 15, 5),
    ],
)
def test_single_day_boundaries(start_hour, end_hour, hours):
    assert compute_hours(f"{WED}T{start_hour:02d}:00", f"{WED}T{end_hour:02d}:00") == hours


@pytest.mark.parametrize(
    "start_hour, end_hour",
    [
        (13, 18),  # 13 點是午休，不是合法起點
        (18, 18),
        (9, 9),  # 零長度
        (9, 14),  # 14 點不是合法終點
        (10, 9),  # 結束早於開始
        (17, 19),
        (7, 9),
    ],
)
def test_single_day_invalid_range(start_hour, end_hour):
    assert_error("INVALID_TIME_RANGE", f"{WED}T{start_hour:02d}:00", f"{WED}T{end_hour:02d}:00")


# 跨日與週末
@pytest.mark.parametrize(
    "start_at, end_at, hours",
    [
        (f"{WED}T14:00", f"{FRI}T13:00", 4 + 8 + 4),
        (f"{MON}T09:00", "2026-09-25T18:00", 40),  # 週一到週五整週
        (f"{MON}T09:00", "2026-09-28T18:00", 48),  # 跨週末到下週一
        (f"{FRI}T09:00", f"{MON}T18:00", 16),
    ],
)
def test_multi_day(start_at, end_at, hours):
    assert compute_hours(start_at, end_at) == hours


@pytest.mark.parametrize(
    "start_at, end_at",
    [
        (f"{SAT}T09:00", f"{SAT}T18:00"),  # 全週末
        (f"{SAT}T09:00", f"{SUN}T18:00"),
        (f"{FRI}T09:00", f"{SUN}T18:00"),  # 終點在週末
    ],
)
def test_weekend_endpoints_rejected(start_at, end_at):
    assert_error("INVALID_TIME_RANGE", start_at, end_at)


def test_last_representable_date_does_not_overflow():
    assert compute_hours("9999-12-31T09:00", "9999-12-31T10:00") == 1


def test_cross_year_rejected():
    assert_error("CROSS_YEAR_NOT_SUPPORTED", "2026-12-31T09:00", "2027-01-04T10:00")


# 格式
@pytest.mark.parametrize(
    "value",
    [
        f"{WED}T14:00:00",  # 秒數
        f"{WED}T14:00+08:00",  # 時區
        WED,  # 純日期
        f"{WED} 14:00",  # 空白分隔
        f"{WED}T14:30",  # 非整點
        f"{WED}T9:00",  # 小時沒補零
        "2026/09/16T14:00",
        f" {WED}T14:00",
        "2026-02-30T09:00",  # 不存在的日期
        f"{WED}T24:00",
        "２０２６-09-16T09:00",  # 全形年份
        "2026-09-1６T09:00",  # 全形日期
        f"{WED}T0９:00",  # 全形小時
        "٢٠٢٦-09-16T09:00",  # 阿拉伯數字
        f"{WED}T09:00\n",  # 結尾換行
        "",
        None,
        20260916,
    ],
)
def test_parse_time_rejects_bad_format(value):
    with pytest.raises(LeaveError) as exc:
        parse_time(value)
    assert exc.value.error_code == "INVALID_TIME_FORMAT"


def test_parse_time_accepts_strict_format():
    parsed = parse_time(f"{WED}T09:00")
    assert (parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute) == (2026, 9, 16, 9, 0)
    assert parsed.tzinfo is None
