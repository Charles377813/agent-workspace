# 協作任務板

兩個 agent（Claude Code / Codex）都在這份文件同步進度。動工前先看，收工後更新。
決策歷史看 [decisions.md](decisions.md)；每個任務的細節看 [tasks/](tasks/)（用 `tasks/_TEMPLATE.md`）。

狀態值：`TODO` / `IN PROGRESS (claude|codex)` / `BLOCKED` / `DONE (claude|codex)`

## 任務表

| # | 任務 | 狀態 | 分支 | 任務檔 |
|---|------|------|------|--------|
| 1 | 初始化工作區 | DONE (claude) | main | — |
| 2 | 依 Codex review 補強文件與工作流 | DONE (claude) | main | — |

## 交接

（目前沒有待交接事項）

## 待處理（等實作開始）

- 選定語言／框架後，定義 `src/` 與測試目錄結構，README 補測試指令與最低驗證要求。
- 接上 GitHub remote 後，加 PR template。
