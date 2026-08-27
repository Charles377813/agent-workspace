# Skill 創建 SOP

建立或修改 Claude Code / agent skill 的標準流程。範本見 [SKILL-template.md](SKILL-template.md)。

本 SOP 濃縮自官方 `skill-creator`，並套用本工作區的慣例（繁中、雙側同步、祈使語氣）。

---

## 0. 前置判斷：這件事該做成 skill 嗎？

判斷依據（越多項成立越值得做，用來輔助判斷，不是硬性計分）：

- 會**重複**發生，不是一次性任務。
- 有**固定流程或產出格式**，寫下來能省去每次重新推導。
- 目前 Claude 預設行為做不好，或容易漏步驟。
- 職責跟現有 skill **不重疊**（先看 `.claude/skills/` 既有清單）。

大致原則：只有「重複」一項成立、其他都不明顯時，通常還不值得做成 skill；反過來若某一項特別強（例如產出格式很固定、或預設行為常出錯），單項也可能就夠。拿不準時列出你的判斷交使用者定。skill 太多會稀釋觸發準確度、增加維護負擔。

---

## 1. 釐清意圖（動手前先問清楚）

寫任何字之前，把這四題答完：

1. **這個 skill 讓 agent 能做什麼？**（一句話）
2. **什麼時候該觸發？** 使用者會講哪些話、在什麼情境。列出 3～5 個實際說法。
3. **產出格式是什麼？** 檔案？報告？就地修改？固定模板長怎樣。
4. **需要測試案例嗎？** 產出可客觀驗證（檔案轉換、資料抽取、固定步驟）→ 需要；產出主觀（寫作風格、設計）→ 通常不用。

若這個 skill 是要把「當前對話正在做的流程」固化下來，先從對話歷史抽答案：用了哪些工具、步驟順序、使用者做過哪些修正、觀察到的輸入/輸出格式。缺的再問使用者補。

---

## 2. 寫 SKILL.md

用 [SKILL-template.md](SKILL-template.md) 起稿。重點：

### frontmatter

```yaml
---
name: <kebab-case，跟資料夾同名>
description: <觸發判斷的主要依據——寫清楚「做什麼」+「何時用」+ 邊界>
---
```

**description 是觸發的主要選擇訊號**（agent 靠它決定要不要載入這個 skill），body 在觸發前不會被讀到。所以所有「何時使用」的資訊都寫在 description，不要只放 body。各平台比對 description 的細節不同，別假設某段字在 Claude Code 與其他 agent 上觸發行為完全一致——寫清楚使用時機與邊界，再靠第 5 節實測。

- 要「推一點」：Claude 傾向**低估觸發**（該用時沒用）。與其寫「整理影片筆記的方法」，寫「…使用時機：使用者貼 YouTube 連結、或說『幫我查核這支影片』『整理這支影片的筆記』時套用」。
- 講清楚**邊界**：什麼情況**不**該用（近義但不同的任務），減少誤觸發。

### body

- **祈使語氣**：「逐一檢查 X」而非「這個 skill 會檢查 X」。
- **解釋為什麼**：與其一堆全大寫 MUST，不如說明這步為何重要，讓模型能舉一反三。看到自己在寫 `ALWAYS`/`NEVER` 全大寫就是警訊——換個說法解釋原因。
- **長度**：SKILL.md 越短越好，多數落在幾十行到一兩百行。長到讀起來像參考手冊（數百行以上）就是該拆的訊號：主檔留流程與選擇邏輯，細節拆到 `references/`，並在主檔明確指路「X 情況去讀 references/x.md」。行數是判斷訊號不是硬上限——真的必要的長內容不用硬砍。
- **固定產出格式**用模板寫死：

  ```markdown
  ## 報告結構
  一律用這個模板：
  # [標題]
  ## 摘要
  ## 發現
  ## 建議
  ```

- **範例**：加幾個 Input/Output 範例通常很有幫助，尤其產出格式需要抓分寸時。數量看需要——夠讓模型抓到分寸就好，不用湊數。

### 三層漸進揭露（progressive disclosure）

| 層 | 內容 | 何時載入 |
|---|---|---|
| 1 | name + description | 永遠在 context（~100 字） |
| 2 | SKILL.md body | 觸發時載入（越短越好，過長就拆 references/） |
| 3 | `references/`、`scripts/`、`assets/` | 需要時才讀；script 可直接執行不佔 context |

多領域/框架的 skill 按變體拆 `references/`（如 `aws.md` / `gcp.md`），主檔只留選擇邏輯。

### 目錄結構

```
skill-name/
├── SKILL.md          （必要）
├── scripts/          （選用）重複性/確定性工作的可執行腳本
├── references/       （選用）需要時載入的文件
└── assets/           （選用）產出用的模板、圖示、字型
```

---

## 3. 雙側同步（本工作區慣例）

本工作區的決定是：**同一個 skill 由兩側共用一份內容**，不是各平台各自維護。

- Claude Code 自動載入 `.claude/skills/<name>/`。
- 其他 agent（Codex 等）沒有引擎級的專案 skill 自動發現；靠 `AGENTS.md` 明文指路到 `.agents/skills/<name>/`，並要求「符合用途時動手前先完整讀取該 skill」。新增 skill 時，若 `AGENTS.md` 的可用 skill 清單需要補一行，一併補。

**改任何一個 skill（新增／修改／刪除），兩側必須完全一致**：不是只複製 `SKILL.md`，而是複製**整個 `<name>/` 目錄**——含 `scripts/`、`references/`、`assets/`、以及任何 agent 專屬設定檔。只同步主檔會讓另一側缺相依資源而靜默失效。刪除 skill 則兩側目錄都刪，並在 `AGENTS.md` 移除對應清單行。

檢查（PowerShell 沒有 `diff -r`，用 git，兩側環境都有）：

```bash
git diff --no-index --stat -- .claude/skills .agents/skills
echo "exit=$?"
```

- `exit=0`：兩側檔案樹與內容完全一致。
- `exit=1`：有差異，`--stat` 列出是哪些檔。
- 某側目錄不存在時會明確報 `does not exist`，不會靜默當成一致。

---

## 4. 測試（產出可客觀驗證時）

1. 寫幾個**真實使用者會講的**測試 prompt（具體、有細節、有情境，不是「格式化這份資料」這種抽象句）。
2. 給使用者過目：「這幾個測試案例對嗎，要加嗎？」
3. 每個測試 prompt 在**乾淨 session**跑（一個 prompt 一個新 session，不要同一個 session 連跑，脈絡殘留會讓結果失真）。不要用「同 session 先問不帶 skill、再問帶 skill」當 baseline——那不是有效對照。改為核對可觀察的不變量：
   - **有沒有被觸發**：只給一句符合 description 的請求，確認 skill 有載入。
   - **產出符不符合規格**：逐項對照「產出格式」模板、該產生的檔案／回報有沒有出現。
   - **前置條件失敗路徑**：故意讓來源不滿足（空資料夾等），確認 skill 按規定回報「沒東西可做」，而不是硬生成。
   - 需要跟「沒有這個 skill 時」比較時，用另一個乾淨 session、完全不裝該 skill 跑同一個 prompt，而不是在同 session 內切換。
4. 讓使用者看實際產出再決定怎麼改。

改進原則：

- **從回饋一般化**：skill 要能用在千百種 prompt，不是只過那幾個測試案例。別加 overfit 的碎規則。
- **保持精簡**：拿掉沒在出力的內容。讀 transcript 不是只看最終產出——若 skill 害模型繞路，把那段拿掉再試。
- **重複工作 → 包成 script**：若每個測試案例模型都自己寫了類似的 helper script，那就寫一次放 `scripts/`，叫 skill 直接用。

---

## 5. 觸發詞優化（description tuning）

skill 寫好後，可針對 description 做觸發測試：

1. 造 ~20 個 query，一半該觸發、一半不該（重點是**近義但不該觸發**的邊界案例）。
2. 每個 query 跑 3 次看觸發率。
3. 依失敗案例改 description，重測。

注意：簡單的一步任務（「讀這個 PDF」）本來就不會觸發 skill，因為 Claude 直接做就好。測試 query 要夠複雜到 agent 真的需要查 skill。

---

## 6. 完成檢查清單

- [ ] `name` = 資料夾名 = kebab-case
- [ ] `description` 含「做什麼」+「何時用」+ 邊界，語氣夠明確
- [ ] body 祈使語氣、有解釋 why、無多餘全大寫 MUST
- [ ] SKILL.md 夠精簡；長到像參考手冊的部分已拆 `references/` 並指路
- [ ] 固定產出格式已用模板寫死
- [ ] 職責跟既有 skill 不重疊
- [ ] 兩側整個 `<name>/` 目錄一致（`git diff --no-index --stat -- .claude/skills .agents/skills` exit 0）；`AGENTS.md` 清單已同步（新增/刪除時）
- [ ] 測試 prompt 在乾淨 session 跑過（若適用），使用者已確認產出
- [ ] `docs/decisions.md` 記一行：新增/修改/刪除了哪個 skill、為什麼
