# CLAUDE.md

給 Claude Code 的專案指引。Codex 對應的檔案是 `AGENTS.md`，內容應保持一致。

## 這個專案是什麼

Claude Code 與 Codex 協作的工作區。多個 agent 會在同一個 repo 上輪流作業。

## 開工前

1. 讀 `docs/coordination.md`，確認沒有其他 agent 正在做同一件事。
2. 在 coordination.md 的任務表新增或更新你的條目：狀態改成 `IN PROGRESS (claude)`。

## 作業慣例

- 每個任務開新分支：`feat/<簡述>` 或 `fix/<簡述>`，不要直接改 `main`。
- 只有使用者明確要求時才 commit / push。
- commit message 結尾加：`Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`
- 暫存與實驗檔放 `scratch/`（已被 gitignore）。

## 收工後

- coordination.md 對應條目改成 `DONE (claude)`，補一行：完成了什麼、動到哪些檔案、還有什麼待辦。
- 有交接給 Codex 的事項，寫在 coordination.md 的「交接」區。
