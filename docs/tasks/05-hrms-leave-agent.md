# 任務：HRMS 自然語言請假 Agent

- **編號**：#5（對應 coordination.md）
- **負責 agent**：claude
- **分支**：`feat/hrms-leave-agent`（實作時開）
- **狀態**：IN PROGRESS（架構已過 Codex review 兩輪；子項 2 時間規則、子項 3 服務層已實作）

## 目標

請假原本要到 HR 平台網頁登記；改成一句自然語言就完成請假，送出前由人確認。

## 驗收條件

設計細節以 [src/hrms-leave-agent/README.md](../../src/hrms-leave-agent/README.md) 為準。

**資料與環境**
- [x] `init_db.sql` 可建出五張表與種子資料，整份在一個交易內、重跑完整重置（含 `audit_logs`）
- [ ] `requirements.txt` 固定 `mcp>=1.28,<2`，照 README 初始化步驟可跑起來

**Server／服務層**
- [ ] MCP Server 提供 `query_leave_balance`、`preview_leave`、`apply_leave` 三個工具，皆以 `employee_id` 為必填參數
- [x] 時間規則照 README：嚴格格式、合法邊界不裁切、半開區間、只計平日、同年度
- [x] 重疊判斷用半開區間、不分假別；相鄰不算重疊
- [x] 每個連線 `isolation_level=None` ＋ `PRAGMA foreign_keys = ON`
- [x] `apply_leave` 在 `BEGIN IMMEDIATE` 交易內重新驗證、重疊檢查、條件式扣抵、寫假單、寫稽核；任何失敗完整回滾
- [x] 鎖定回 `DB_BUSY`、未預期例外回 `INTERNAL_ERROR`，不外洩原始 exception
- [x] 無額度資料在 preview 與 apply 都回 `INSUFFICIENT_BALANCE`

**Client**
- [ ] 給 LLM 的 schema：`employee_id` 同時不在 `properties` 與 `required`；`preview_leave` 不給 LLM
- [ ] 所有 tool_call 無條件以 `--employee` 覆蓋 `employee_id`
- [ ] 攔下 `apply_leave` 後凍結參數 → Client 自行呼叫 `preview_leave` → 顯示確認 → y 用同一份參數送出；N 回 `USER_REJECTED`；preview 失敗不詢問、直接回錯誤給 LLM
- [ ] `--today` 可固定日期
- [ ] 一句「下週三下午請特休」能走完整流程（固定 `--today`）

## 驗證方式

`pytest src/hrms-leave-agent/tests`，至少涵蓋：

- **時間規則**：README 時數對照表每一列；另加 09:00、13:00、14:00、18:00 邊界、秒數／時區／純日期格式、全週末、跨年度、零工時
- **服務層**：成功請假三表變化正確；餘額剛好等於時數可成功；餘額不足；無額度資料；員工／假別不存在；重疊與剛好相鄰
- **回滾**：讓 `audit_logs` 寫入失敗（例如 monkeypatch 或 trigger），確認 `leave_balances` 與 `leave_requests` 都沒變
- **併發**：另一條連線持有寫入鎖時，`apply_leave` 回 `DB_BUSY` 且資料不變
- **Client hook**（mock Server，不連 LLM）：schema 轉換結果；LLM 帶假 `employee_id` 被覆蓋；確認畫面資料來自 apply 的實際參數；按 N 後三表不變

手動（連 LLM，固定 `--today 2026-09-13`）：成功請假、按 N 取消、E002 特休餘額不足。

## 異動檔案

- `src/hrms-leave-agent/README.md`、`init_db.sql`、`requirements.txt`、`.env.example`、`.gitignore`（設計與骨架）
- `src/hrms-leave-agent/leave_service.py`、`pytest.ini`、`tests/conftest.py`、`tests/test_time_rules.py`、`tests/test_leave_service.py`（子項 2、3）

## 交接事項

- 時間盒：約 8 個晚上（週二、四，2026-09-15～10-08），超過就砍功能，不侵占週日 RHCSA 時段。
- 可再砍的：`query_leave_balance`（非請假主流程）；砍了要同步改驗收條件。
- 不要新增：國定假日套件、auth、主管簽核、server-side elicitation、approval token、網頁 UI、ORM。
- 程式骨架參考 `C:\dev\ai-agent\mcp_server.py`、`mcp_client.py`（MCP SDK v1 寫法）；HITL 參考 `lesson5-4.py`（`mcp_client.py` 本身沒實作攔截）。
- Codex review 第 1 輪（2026-09-13）8 項＋驗收缺口全部採納，決策摘要見 `docs/decisions.md`。
