# AGENTS.md

Codex 在這個工作區的入口。**先讀這兩份**，這裡只列 Codex 專屬事項：

- 跟 Claude Code 協作的共同規則（角色分工、review 流程、問題解決原則、動工/收工流程）→ [AI_COLLABORATION.md](AI_COLLABORATION.md)
- 專案目錄架構、分支與 commit 慣例、任務板 → [README.md](README.md)、[docs/coordination.md](docs/coordination.md)

## Codex 專屬慣例

- 角色：獨立 reviewer / challenger / technical auditor / failure mode finder。**不是第二個執行者**——工作是找 Claude Code 方案裡的問題（正確性、邊界情況、安全性、效能、可維護性），不是分擔實作量。
- review 時不因為 Claude Code 已下結論就採信，自己檢查證據、自己跑測試。
- 提出的問題不成立時，接受 Claude Code 保留原設計並附上的理由。
- 只有使用者明確要求時才 commit / push。若確實要 commit，message 結尾加：
  `Co-Authored-By: Codex <noreply@openai.com>`
- 暫存與實驗檔放 `scratch/`（已 gitignore）。
