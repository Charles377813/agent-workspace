# agent-workspace

Claude Code 與 Codex 協作用的專案工作區。

## 目錄結構

| 路徑 | 用途 |
|---|---|
| `AI_COLLABORATION.md` | Claude Code / Codex 協作的共同規則來源（角色分工、review 流程） |
| `CLAUDE.md` | Claude Code 專屬入口 |
| `AGENTS.md` | Codex 專屬入口 |
| `docs/coordination.md` | 共用任務板 / 交接記錄 |
| `docs/decisions.md` | 技術選型與流程決策歷史 |
| `docs/tasks/` | 每個任務一個檔，用 `_TEMPLATE.md` |
| `docs/guides/` | 可重複流程的 SOP（如 skill 創建） |
| `src/` | 實際程式碼 |
| `scratch/` | 暫時檔案、實驗、草稿（不進版控） |

## 協作原則

- **分工**：Claude Code 做分析／設計／實作／refactor；Codex 當獨立 reviewer 找問題，不是第二雙手。完整說明見 `AI_COLLABORATION.md`。
- 動工前先看 `docs/coordination.md` 的任務狀態，標 `IN PROGRESS`；完成標 `DONE` 並寫摘要與異動檔案。
- 每個功能開獨立 git 分支（`feat/*` / `fix/*`），別直接在 `main` 上改。
- 只有使用者明確要求時才 commit / push。
