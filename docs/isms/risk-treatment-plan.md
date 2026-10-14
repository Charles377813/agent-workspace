# 風險處理計畫（對應條款 6.1.3 e）

> 練習作品，不是組織的 ISMS，不宣稱符合 ISO/IEC 27001。負責人皆為作者本人（無組織職務區隔）。處理選項：修改（降低風險）、接受、避免、轉移。

更新日期：2026-10-14。風險編號與評分見 [risk-register.md](risk-register.md)。

| ID | 處理選項 | 對應 Annex A | 處理內容 | 狀態 | 證據 |
|---|---|---|---|---|---|
| R1 | 接受（暫時） | 5.16、5.18、8.5 | demo 在虛構資料、本機執行下書面接受；上線前需接認證 | 未處理，已書面接受 | `src/hrms-leave-agent/README.md`「已知限制」 |
| R2 | 接受（暫時） | 5.15、8.3 | 同上；上線前需 Server 端授權與核准憑證 | 未處理，已書面接受 | 同上 |
| R3 | 接受 | 5.15 | 維持現有控制，持續以測試守住 | 維持 | `tests/test_client_hitl.py` |
| R4 | 修改（計畫） | 8.29 | 補語意層注入測試、限制 `reason` 長度 | 第二階段 | — |
| R5 | 修改 | 8.15 | `audit_logs` 改為結構化欄位（`actor`、`outcome`、`error_code`、`leave_request_id`）；成功與失敗的 `apply_leave` 都記；失敗紀錄在業務交易回滾後另開交易寫；`BEFORE UPDATE`／`BEFORE DELETE` trigger 擋下修改與刪除；`init_db.sql` 重置流程改為 DROP TABLE 重建並順便升級舊 schema | 已處理（殘餘見下） | `tests/test_audit_log.py`（13 個測試）；變異測試：移除失敗紀錄、停用 trigger 後測試均失敗 |
| R6 | 接受 | — | 維持 | 維持 | `tests/test_leave_service.py` 併發與回滾測試 |
| R7 | 修改（計畫） | 5.12、5.19、5.23 | 資料分類、供應商評估、最小化送出欄位 | 第二階段 | — |
| R8 | 修改（部分） | 5.17、8.4 | gitleaks 掃完整 git 歷史（0 發現）；GitHub Actions 每次 push 掃描 | 部分處理 | `evidence/gitleaks-2026-10-14.txt`、`.github/workflows/security.yml` |
| R9 | 接受 | — | 展示完即關閉 | 維持 | — |
| R10 | 修改（計畫） | 8.13 | 備份與還原演練 | 第二階段 | — |
| R11 | 修改 | 5.21、8.8 | 產生 `requirements.lock`（78 個固定版本）；pip-audit 本機與 CI 掃描，0 個已知漏洞 | 已處理（殘餘見下） | `evidence/pip-audit-2026-10-14.txt`、`src/hrms-leave-agent/requirements.lock` |
| R12 | 接受 | — | 觀察即可 | 維持 | — |
| R13 | 修改（計畫） | 8.10、5.34 | 訂保存期限與刪除規則 | 第二階段 | — |

## 殘餘風險與限制（刻意寫明）

- **R5 的 trigger 防不了有 DB 檔寫入權的人**。SQLite 沒有權限分離，能開啟 `leave.db` 的人可以 `DROP TRIGGER` 後再改紀錄。trigger 擋的是應用程式誤改與一般竄改。要防後者，需把稽核紀錄即時送到外部（例如獨立的日誌服務），列為後續評估項目。
- R5 目前只記 `apply_leave` 的成功與失敗；使用者在確認畫面按取消、以及查詢動作發生在 Client 端，沒有紀錄。`actor` 是呼叫端宣稱的 `employee_id`，在 R1 補上認證前不等於已驗證身分。
- R8：CI 掃描是事後偵測；repo 為公開，金鑰一旦被推上去就已經外洩。尚無 pre-commit 阻擋，也沒有金鑰輪替流程。
- R11：pip-audit 只能找出已公開的漏洞；鎖定檔需要定期更新，目前沒有排程。
- R1、R2（高風險）本次未處理，需要認證與 Server 端授權設計，列為上線前必須處理項目。
