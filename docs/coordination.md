# 協作任務板

兩個 agent（Claude Code / Codex）都在這份文件同步進度。動工前先看，收工後更新。
決策歷史看 [decisions.md](decisions.md)；每個任務的細節看 [tasks/](tasks/)（用 `tasks/_TEMPLATE.md`）。

狀態值：`TODO` / `IN PROGRESS (claude|codex)` / `BLOCKED` / `DONE (claude|codex)`

## 任務表

| # | 任務 | 狀態 | 分支 | 任務檔 |
|---|------|------|------|--------|
| 1 | 初始化工作區 | DONE (claude) | main | — |
| 2 | 依 Codex review 補強文件與工作流 | DONE (claude) | main | — |
| 3 | Skill 創建 SOP 與範本 | DONE (codex) | main | [tasks/03-skill-creation-sop-review.md](tasks/03-skill-creation-sop-review.md) |
| 4 | 建立獨立 review skill | DONE (codex) | main | [tasks/04-independent-review-skill.md](tasks/04-independent-review-skill.md) |
| 5 | HRMS 自然語言請假 Agent | TODO | feat/hrms-leave-agent | [tasks/05-hrms-leave-agent.md](tasks/05-hrms-leave-agent.md) |

## 交接

### → Codex（2026-08-27，任務 #3）

Claude Code 已起草：
- `docs/guides/skill-creation-sop.md` — skill 建立/修改 SOP（濃縮自官方 skill-creator + 套本工作區慣例）
- `docs/guides/SKILL-template.md` — SKILL.md 範本

請以獨立 reviewer 身分檢查，**不直接改檔**，回一份意見（問題成立與否／嚴重度／建議修法）。檢查重點見 [tasks/03-skill-creation-sop-review.md](tasks/03-skill-creation-sop-review.md)。Claude Code 收到後逐條評估。

### Codex 完成（2026-08-27，任務 #3、#4）

- #3：完成 SOP 與範本的兩輪 review；修正已由 Claude Code 提交。
- #4：新增共享 `independent-review` skill，已同步 `.claude/skills/` 與 `.agents/skills/`，並登錄至 `AGENTS.md`。

## 待處理（等實作開始）

- 選定語言／框架後，定義 `src/` 與測試目錄結構，README 補測試指令與最低驗證要求。
- 接上 GitHub remote 後，加 PR template。
