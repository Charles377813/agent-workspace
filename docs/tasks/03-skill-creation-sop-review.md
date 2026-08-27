# 任務：Codex review — Skill 創建 SOP 與範本

- **編號**：#3（對應 coordination.md）
- **負責 agent**：codex（review）
- **分支**：main（純文件，未開分支）
- **狀態**：TODO — 等 Codex review

## 目標

Claude Code 已起草 skill 建立/修改的 SOP 與 SKILL.md 範本，供本工作區之後產出 skill 時共用。請 Codex 以獨立 reviewer 身分檢查，不是補寫內容。

## Review 範圍

- `docs/guides/skill-creation-sop.md`
- `docs/guides/SKILL-template.md`

## 希望 Codex 檢查的點

1. **流程完整性**：從「該不該做成 skill」到「完成檢查清單」，有沒有漏掉的關鍵步驟或順序錯誤。
2. **可執行性**：每一步是否夠具體到 agent 能照做，還是有含糊、需要腦補的地方。
3. **與官方 skill-creator 的落差**：本 SOP 是濃縮版，Codex 判斷有沒有為了精簡而砍掉不該砍的東西（例如 eval 的嚴謹度、baseline 比較的必要性）。
4. **雙側同步規則**（SOP 第 3 節）：`.claude/skills/` ↔ `.agents/skills/` 的假設對 Codex／其他 agent 是否成立；`diff -r` 檢查是否可靠。
5. **範本實用性**：SKILL-template.md 照著填，產出的 SKILL.md 會不會有結構問題；註解行的標示方式（「說明：」）會不會有人忘了刪。
6. **邊界情況**：修改既有 skill（非新建）、刪除 skill、skill 之間職責重疊時的處理，SOP 講得夠不夠。

## 交付方式

Codex 回一份 review 意見（問題成立與否、嚴重度、建議修法），**不直接改檔**。Claude Code 收到後逐條評估，成立的修、不成立的保留原設計並說明理由。

## 驗證方式

Review 完成後，挑一個實際要做的 skill，兩個 agent 各照這份 SOP 走一次，看流程有沒有卡點。
