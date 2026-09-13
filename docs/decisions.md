# 決策記錄

專案的技術選型與流程決策，一條一行，附日期與理由。從 `coordination.md` 分出來，避免任務板同時扛任務／交接／決策三件事。

- **2026-08-27**：工作區與 `C:\Obsidian Vault` 分開（vault 管知識，這裡管程式），但協作模型沿用 vault 的 `AI_COLLABORATION.md`——Claude Code = 實作者、Codex = 獨立 reviewer。
- **2026-08-27**：`docs/` 分為 `tasks/`（每任務一檔，用 `_TEMPLATE.md`）、`decisions.md`（本檔）、`coordination.md`（任務板 + 交接）。`guides/` 待有內容再建。
- **2026-08-27**：緩辦——`src/` 與測試目錄結構、README 測試指令，等選定語言／框架後在首個實作任務一併定義（無法在空 repo 先定）。
- **2026-08-27**：新增 `docs/guides/`，放 skill 創建 SOP 與 SKILL.md 範本（沿用 vault 的雙側同步慣例 `.claude/skills` ↔ `.agents/skills`）。交 Codex review（任務 #3）。
- **2026-08-27**：緩辦——PR template（`.github/`），等接上 GitHub remote 再加，內容要求：問題背景、驗證結果、風險、Codex review 結果。
- **2026-08-27**：skill 創建 SOP（任務 #3）Codex re-review 後定案——(a) 雙側 `.claude/skills` ↔ `.agents/skills` 是「共用同一份、必須同步」，同步對象是整個 skill 目錄含 `scripts/`/`references/`/`assets/`，不是只 `SKILL.md`；(b) `diff -r` 不跨平台，改用 `git diff --no-index`；(c) 硬性門檻（「至少兩項」、SKILL.md「500 行」、「2～3 範例」）改為判斷訊號，不設硬上限；(d) 測試 baseline 改為乾淨 session + 可觀察不變量，不用同 session 帶／不帶對照；(e)「description 是唯一觸發機制」改為「主要選擇訊號」，不宣稱跨平台觸發一致。
- **2026-08-27**：skill SOP 第 2 輪 focused re-review 修正——(f) `AGENTS.md` 建立 `skill-list` 具名清單（`<!-- skill-list:start/end -->` 標記），新增／刪除 skill **必須**同步該清單，非「需要才補」；(g) 一致性檢查的 exit code 判斷分 Bash（`$?`）與 PowerShell（`$LASTEXITCODE`，因 PS 的 `$?` 是布林值）兩版。
- **2026-08-27**：新增共享 `independent-review` skill，供程式、文件與變更的獨立審查使用；原則是以實際證據回報問題，不直接實作修正。
- **2026-09-13**：結束 08-27 的緩辦「`src/` 與測試目錄結構」——每個專案放 `src/<專案名>/`，自帶 `README.md`（架構＋執行＋測試指令）、`tests/`、`.gitignore`；協作規則一律沿用工作區的 `AI_COLLABORATION.md`／`CLAUDE.md`／`AGENTS.md`，不在專案內另建。等 `src/` 出現技術棧或規則明顯不同的第二個專案時，才在該專案內加只寫專屬事項的 `CLAUDE.md`，共同規則仍引用工作區。首例：`src/hrms-leave-agent/`（任務 #5）。
- **2026-09-13**：任務 #5 HRMS 請假 Agent 架構經 Codex review 第 1 輪後定案——(a) MCP Python SDK 固定 v1（`mcp>=1.28,<2`），因 v2 改名 `MCPServer` 與 Client API，沿用 Lesson 5 寫法且守住 8 晚時間盒；(b) HITL 綁定實際參數：Client 攔下 `apply_leave` 後凍結參數、自行 `preview_leave`、確認後以同一份參數送出，`preview_leave` 不給 LLM；(c) `employee_id` 從 LLM schema 移除並由 Client 無條件覆蓋；(d) SQLite 連線 `isolation_level=None` ＋ 每連線 `PRAGMA foreign_keys = ON`，交易失敗完整回滾並回 `DB_BUSY`／`INTERNAL_ERROR`；(e) 時間用半開區間、合法邊界不裁切、嚴格格式；(f) 砍掉 `list_leave_types`，假別改用 schema `enum`；(g) HITL 保證範圍限定為官方 Client 路徑。
