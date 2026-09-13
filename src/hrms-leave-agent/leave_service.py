"""請假業務邏輯：時間規則、時數計算（之後補驗證、餘額、重疊、寫入交易）。

不依賴 MCP 與 LLM，可直接單元測試。規則說明見 README「時間與時數規則」。
"""

import re
from datetime import date, datetime, time, timedelta

TIME_FORMAT = "%Y-%m-%dT%H:%M"
# 用 [0-9] 而非 \d：\d 會匹配全形等 Unicode 數字，strptime 也會接受
_TIME_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:00")

# 工作時段，半開區間 [start, end)
WORK_BLOCKS = ((time(9), time(13)), (time(14), time(18)))
VALID_START_HOURS = frozenset({9, 10, 11, 12, 14, 15, 16, 17})
VALID_END_HOURS = frozenset({10, 11, 12, 13, 15, 16, 17, 18})


class LeaveError(Exception):
    """業務錯誤，error_code 對應 README 的錯誤碼表。"""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def parse_time(value: str) -> datetime:
    """嚴格解析 YYYY-MM-DDTHH:00；秒數、時區、純日期、非整點一律拒絕。"""
    if not isinstance(value, str) or not _TIME_PATTERN.fullmatch(value):
        raise LeaveError("INVALID_TIME_FORMAT", f"時間格式須為 YYYY-MM-DDTHH:00：{value!r}")
    try:
        # 正則只管形狀，不存在的日期（2026-02-30）與 24 點靠 strptime 擋
        return datetime.strptime(value, TIME_FORMAT)
    except ValueError:
        raise LeaveError("INVALID_TIME_FORMAT", f"不存在的日期或時間：{value!r}") from None


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def compute_hours(start_at: str, end_at: str) -> int:
    """回傳請假區間 [start_at, end_at) 內的工作時數。

    起訖必須是平日的合法邊界，不裁切；只計平日的 09–13、14–18。
    """
    start = parse_time(start_at)
    end = parse_time(end_at)

    if start.year != end.year:
        raise LeaveError("CROSS_YEAR_NOT_SUPPORTED", "不支援跨年度請假，請分成兩張假單")
    if end <= start:
        raise LeaveError("INVALID_TIME_RANGE", "結束時間必須晚於開始時間")
    if not _is_weekday(start.date()) or start.hour not in VALID_START_HOURS:
        raise LeaveError("INVALID_TIME_RANGE", f"開始時間不是平日的合法時段起點：{start_at}")
    if not _is_weekday(end.date()) or end.hour not in VALID_END_HOURS:
        raise LeaveError("INVALID_TIME_RANGE", f"結束時間不是平日的合法時段終點：{end_at}")

    total = timedelta()
    # 以天數位移迭代，不在最後一天之後再加一天（避免 9999-12-31 溢位）
    for offset in range((end.date() - start.date()).days + 1):
        day = start.date() + timedelta(days=offset)
        if not _is_weekday(day):
            continue
        for block_start, block_end in WORK_BLOCKS:
            overlap = min(end, datetime.combine(day, block_end)) - max(
                start, datetime.combine(day, block_start)
            )
            if overlap > timedelta():
                total += overlap

    hours = int(total.total_seconds() // 3600)
    if hours <= 0:
        raise LeaveError("INVALID_TIME_RANGE", "請假區間內沒有任何工作時數")
    return hours
