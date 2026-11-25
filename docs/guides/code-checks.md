# 寫程式時的檢查問題（測試與模組設計）

動手寫測試或設計新模組時翻一下。這是檢查問題，不是強制規則，也不要求回頭改既有程式。

## 寫測試（TDD 三條）

1. **先講好測哪個公開介面。** 寫第一個測試前，說清楚從哪個介面觀察行為、抓得到什麼、漏掉什麼；不去測內部實作。
2. **期望值要有獨立來源。** 用寫死的已知值、手算的例子或規格表，不用和程式相同的算法再算一次（那種測試不可能失敗）。本工作區的例子：`src/hrms-leave-agent/README.md` 的時數對照表。
3. **一次一個測試、一段實作。** 先看到紅，再寫剛好讓它變綠的程式；不要先寫完所有測試。

## 設計新模組（深模組）

深模組＝介面小、藏在後面的實作多。設計時問：

1. 介面能不能更小（方法更少、參數更簡單）？
2. 能不能把複雜度藏進模組裡，不要求呼叫端知道？

## 出處

- Matt Pocock〈Software Fundamentals Matter More Than Ever〉：<https://www.youtube.com/watch?v=v4F1gFy-hqg>
- [mattpocock/skills 的 tdd](https://github.com/mattpocock/skills/blob/main/skills/engineering/tdd/SKILL.md)（MIT；只借上面三條，沒有安裝）

專案用詞見各專案的 `GLOSSARY.md`（目前：[hrms-leave-agent](../../src/hrms-leave-agent/GLOSSARY.md)）。
