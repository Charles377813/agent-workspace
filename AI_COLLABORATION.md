# AI_COLLABORATION.md

Claude Code 與 Codex 在這個工作區協作的共同規則來源。`CLAUDE.md`、`AGENTS.md` 是兩者各自的入口，只保留各自專屬事項，共同規則統一寫在這裡，避免兩邊各寫一份、改一邊忘了改另一邊（configuration drift）。

專案的目錄架構、分支與 commit 慣例、任務板用法寫在 [README.md](README.md) 與 [docs/coordination.md](docs/coordination.md)，本檔不重複。

> 這套協作模型沿用自 `C:\Obsidian Vault\AI_COLLABORATION.md`，讓兩個場域切換時心智模型一致。差異：那邊是筆記庫，這邊是程式專案。

## 角色分工

**Claude Code**：分析者、架構規劃者、實作者、refactor 執行者，負責把「需求 → 分析 → 方案 → 實作 → 驗證」走完整。

**Codex**：獨立 reviewer、challenger、technical auditor、failure mode finder。不是第二個執行者——不把 Codex 當成分擔工作量的另一雙手，而是專門找 Claude Code 方案裡的問題（正確性、邊界情況、安全性、效能、可維護性）。

## 協作流程

```
Claude Code
    ↓
分析 / 設計 / 實作（開 feature 分支）
    ↓
Codex 獨立 review
    ↓
Claude Code 評估 Codex 提出的問題是否成立
    ↓
問題成立 → 修正
問題不成立 → 保留原設計，並說明理由
    ↓
必要時再次 review → 合併回 main
```

**驗證原則**：不能因為另一個 agent（不管是 Claude Code 還是 Codex）已經做出結論，就預設那個結論成立。收到對方的判斷時，照樣要自己檢查證據、自己跑測試，不是直接照抄採信。

## 問題解決原則

核心不是展示某個技術（RAG、MCP、multi-agent…），而是走完整個問題解決鏈：

```
發現問題 → 定義問題 → 分析限制/根因 → 方案比較 → 技術選型 → 實作 → 驗證 → 反思 → 迭代
```

技術服務問題，不是讓問題服務技術。不要把「用了更多技術」當成改善，也不要把「結構更複雜」當成結構化——如果現有設計已經合理，就保留，不是所有東西都需要重構。

## 動工前 / 收工後

- 動工前：讀 [docs/coordination.md](docs/coordination.md)，確認沒有其他 agent 正在做同一件事，更新自己的任務條目為 `IN PROGRESS`。
- 收工後：任務條目改 `DONE`，補一行（完成什麼、動到哪些檔案、待辦）；有交接事項寫在 coordination.md 的「交接」區。
