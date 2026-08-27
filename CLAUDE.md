# CLAUDE.md

Claude Code 在這個工作區的入口。**先讀這兩份**，這裡只列 Claude Code 專屬事項：

- 跟 Codex 協作的共同規則（角色分工、review 流程、問題解決原則、動工/收工流程）→ [AI_COLLABORATION.md](AI_COLLABORATION.md)
- 專案目錄架構、分支與 commit 慣例、任務板 → [README.md](README.md)、[docs/coordination.md](docs/coordination.md)

## Claude Code 專屬慣例

- 角色：分析 / 設計 / 實作 / refactor，走完整條問題解決鏈（見 AI_COLLABORATION.md）。
- 每個任務開新分支 `feat/<簡述>` 或 `fix/<簡述>`，不直接改 `main`。
- 只有使用者明確要求時才 commit / push。commit message 結尾加：
  `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`
- 實作前設計已核准的話，用 `worktree-implementation-start` skill 開隔離 worktree。
  該 skill 是使用者環境既有的 Claude Code skill（`~/.claude/skills/` 與 `C:\Obsidian Vault\.claude\skills\`）。
  沒有該 skill 時的 fallback：`git worktree add ../agent-workspace-<branch> -b <branch>`，
  在該 worktree 跑完初始化與測試、確認乾淨狀態能通過後再動工。
- 暫存與實驗檔放 `scratch/`（已 gitignore）。
- 交出去給 Codex review 前，自己先跑過測試、確認能建置。
