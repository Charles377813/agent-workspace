# agent-workspace

Claude Code 與 Codex 協作用的專案工作區。

## 目錄結構

| 路徑 | 用途 |
|---|---|
| `CLAUDE.md` | Claude Code 讀的專案規範與慣例 |
| `AGENTS.md` | Codex（及其他 agent）讀的專案規範 |
| `docs/coordination.md` | 共用任務板 / 交接記錄，兩邊都在這裡同步進度 |
| `src/` | 實際程式碼 |
| `scratch/` | 暫時檔案、實驗、草稿（不進版控） |

## 協作原則

1. 動工前先看 `docs/coordination.md` 目前的任務狀態。
2. 開始一項任務 → 在 coordination.md 標記 `IN PROGRESS` 與負責的 agent。
3. 完成 → 標記 `DONE` 並寫一行摘要與異動檔案。
4. 每個功能開獨立 git 分支，別直接在 `main` 上改。
