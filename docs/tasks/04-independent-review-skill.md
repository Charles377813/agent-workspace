# 任務：建立獨立 review skill

- **編號**：#4（對應 coordination.md）
- **負責 agent**：codex
- **分支**：main
- **狀態**：DONE

## 目標

建立一個可重複使用的 workspace skill，讓 agent 在程式、文件或變更 review 時保持獨立、證據導向且不越權實作。

## 驗收條件

- [x] `independent-review` 有清楚的觸發條件與不適用範圍。
- [x] `.claude/skills/` 與 `.agents/skills/` 的 skill 目錄內容一致。
- [x] `AGENTS.md` 的具名 skill 清單與 decisions 記錄已同步。

## 驗證方式

比對雙側目錄內容，並確認 SKILL.md 的 frontmatter、觸發範圍及回報格式完整。

## 異動檔案

- `.claude/skills/independent-review/SKILL.md`
- `.agents/skills/independent-review/SKILL.md`
- `AGENTS.md`
- `docs/coordination.md`
- `docs/decisions.md`

## 交接事項

無。
