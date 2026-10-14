# hrms-leave-agent 資訊安全對照文件包（ISO/IEC 27001:2022）

## 聲明

- 這是**練習作品**，對象是 `src/hrms-leave-agent`（終端機版＋本機 Gradio demo，資料為虛構的 3 位員工）。
- **不是組織的 ISMS**：沒有利害關係人分析、法規盤點與管理階層核准，**不宣稱符合、通過或導入 ISO/IEC 27001**。
- 做法是以 ISO/IEC 27001:2022 的風險評鑑與處理流程、Annex A 控制項為對照框架，盤點控制措施與缺口，並實際補強其中幾項、留下可重現的證據。
- 風險評分是作者依本專案脈絡做的初評，準則見 [risk-register.md](risk-register.md)。

## 文件索引

| 文件 | 內容 | 對應條款 |
|---|---|---|
| [risk-register.md](risk-register.md) | 13 項風險，含初評與處理後的殘餘風險；風險準則與接受準則 | 6.1.2 |
| [risk-treatment-plan.md](risk-treatment-plan.md) | 每項風險的處理選項、對應控制、狀態、證據；殘餘風險與限制 | 6.1.3 |
| [annex-a-mapping.md](annex-a-mapping.md) | 27 項 Annex A 控制項對照與統計（**不是**適用性聲明 SoA） | Annex A |
| [evidence/](evidence/) | 測試、secret 掃描、相依套件漏洞掃描的原始輸出 | — |

## 本次補強摘要（2026-10-14）

| 項目 | 結果 | 證據 |
|---|---|---|
| 自動測試 | 補強前 375 個通過；補強後 388 個通過（新增 13 個稽核紀錄測試） | [evidence/pytest-2026-10-14.txt](evidence/pytest-2026-10-14.txt) |
| R5 稽核紀錄 | 成功與失敗都記、欄位結構化、trigger 擋 UPDATE／DELETE | `src/hrms-leave-agent/tests/test_audit_log.py` |
| Secret 掃描 | gitleaks 8.30.1 掃本 repo 所有分支完整歷史，0 發現 | [evidence/gitleaks-2026-10-14.txt](evidence/gitleaks-2026-10-14.txt) |
| 相依套件漏洞 | pip-audit 2.10.1 掃鎖定檔（78 個套件），0 個已知漏洞 | [evidence/pip-audit-2026-10-14.txt](evidence/pip-audit-2026-10-14.txt) |
| 風險數 | 初評 高 2、中 7、低 4；處理後 高 2、中 5、低 6 | [risk-register.md](risk-register.md) |
| Annex A | 初評 已有 4、部分 11、缺口 11、不適用 1；補強後 已有 4、部分 12、缺口 10、不適用 1 | [annex-a-mapping.md](annex-a-mapping.md) |
| CI | GitHub Actions 每次 push 跑 gitleaks 與 pip-audit | `.github/workflows/security.yml` |

高風險 R1（無登入）、R2（Server 端無授權）本次**未處理**，已識別並在 demo 前提下書面接受。

## 如何重現

```bash
cd src/hrms-leave-agent

# 測試（約 2–6 分鐘）
.venv/Scripts/python -m pytest -q

# 相依套件漏洞掃描
pip install pip-audit
pip-audit -r requirements.lock --no-deps --disable-pip

# Secret 掃描（需先安裝 gitleaks，本次使用 8.30.1）
cd ../.. && gitleaks git --redact --no-banner --log-opts="--all" -v .

# 重建鎖定檔
cd src/hrms-leave-agent && uv pip compile requirements-demo.txt requirements.txt --universal --python-version 3.13 -o requirements.lock
```

舊版的 `leave.db` 沒有新的稽核欄位與 trigger，需用 `gradio_app.py --reset-db` 重建。

## 未涵蓋（後續階段）

R4 語意層注入測試、R7 資料分類與供應商評估、R10 備份與還原演練、R13 保存期限；完整適用性聲明（93 項控制）；簡版內部稽核查檢表；稽核紀錄送到外部。
