"""測試用的假 Server：（可選延遲後）寫 PID 檔再睡著，不讀 stdin、不回應 MCP，stdin 關閉也不結束。

用法：python stubborn_server.py <pid_file> [寫 PID 檔前延遲秒數]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo_server_launcher import write_pid_file  # noqa: E402

if len(sys.argv) > 2:
    time.sleep(float(sys.argv[2]))
write_pid_file(Path(sys.argv[1]))
time.sleep(120)
