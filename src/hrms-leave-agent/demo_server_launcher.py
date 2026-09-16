"""Gradio demo 專用的 MCP Server 啟動包裝（任務 #6）。

用法：python demo_server_launcher.py <pid_file>

先把自己的 PID 寫進 pid_file，再在同一個行程裡執行 mcp_server.py。
MCP SDK 的 stdio_client 不對外提供子行程 PID；demo 關閉時若 cleanup 卡住，
需要靠這個檔案強制終止 Server（見 demo_worker.force_kill_from_pid_file）。
mcp_server.py 本身不改。stdout 是 MCP 通道，這裡不能印任何東西。
"""

import os
import runpy
import sys
from pathlib import Path

SERVER_PATH = Path(__file__).resolve().parent / "mcp_server.py"


def write_pid_file(pid_file: Path) -> None:
    # 先寫暫存檔再 replace，讀取端不會讀到寫一半的內容
    temp = pid_file.with_name(pid_file.name + ".tmp")
    temp.write_text(str(os.getpid()), encoding="ascii")
    os.replace(temp, pid_file)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("用法：python demo_server_launcher.py <pid_file>", file=sys.stderr)
        return 2
    write_pid_file(Path(argv[0]))
    sys.argv = [str(SERVER_PATH)]
    runpy.run_path(str(SERVER_PATH), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
