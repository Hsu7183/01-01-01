# 01-01 最新移機交接說明（2026-04-22）

這份是目前最新狀態。移機或交給下一位 AI / 工程師時，請先讀這份，再讀舊的 `00_START_HERE_換電腦.md`。

## 一句話結論

`01-01` 現在已經不是單純找最佳參數，而是往「每週產生候選策略、再用多段驗收與守門規則決定是否使用」發展。

但目前仍不能保證穩定賺錢，也還沒有完全解決過度最佳化。現在這套是篩選與風控流程，不是可直接重倉上線的保證。

## 專案邊界

只處理這個資料夾：

```text
C:\Users\User\Documents\mqquant\01-01
```

不要改：

```text
C:\Users\User\Documents\mqquant\01
C:\Users\User\Documents\mqquant\02
C:\Users\User\Documents\mqquant\xs-core-engine
```

## 啟動方式

```cmd
cd /d C:\Users\User\Documents\mqquant\01-01
run.cmd
```

入口：

```text
C:\Users\User\Documents\mqquant\01-01\app.py
```

目前主 UI：

```text
C:\Users\User\Documents\mqquant\01-01\mq01\ui_runtime_v2.py
```

`run.cmd` 會把 `MQQUANT_SOURCE_ROOT` 指到：

```text
C:\Users\User\Documents\mqquant\01-01\bundle
```

## 新機必搬資料

請搬整個 `01-01` 資料夾，尤其要保留：

```text
bundle\data
bundle\strategy
bundle\param_presets
bundle\run_history
mq01
app.py
run.cmd
```

`bundle\run_history` 很重要，因為裡面有歷史 Top10、歷史最佳、最近策略落盤資料。沒有它，「舊策略 / 新策略守門」就少了 incumbent 參考。

## 目前已完成的主要功能

### 1. 圖表改成比較像報告頁

圖表已從原本深色扁平圖改為白底報告式圖表：

- 資產曲線與每週損益上下排列。
- 圖表高度加大，方便辨識。
- 2020 無交易或前段空白資料會被裁掉。
- 最近 1 年起點有分隔線。
- 每週損益用紅綠柱狀顯示。

### 2. 按開始後不再直接顯示舊結果

之前按「開始執行」後很快顯示完成，主要是畫面抓到舊的快取結果。現在已修正：

- 執行開始會重置 holdout / 分析狀態。
- active job 若存在，不會 fallback 到舊 saved result。
- 測試時要看即時進度與背景程序狀態。

### 3. 參數預設範圍已恢復成大範圍搜尋

最佳策略回填後，左側參數不再全部鎖成最佳值，而是使用 preset 範圍。

目前主要範圍方向：

```text
DonLen              1 ~ 300
ATRLen              1 ~ 100
EMAWarmBars         1 ~ 100
EntryBufferPts      1 ~ 300
DonBufferPts        1 ~ 300
MinATRD             1 ~ 300
ATRStopK            0.01 ~ 3.00
ATRTakeProfitK      0.01 ~ 3.00
MaxEntriesPerDay    1 ~ 100
TimeStopBars        1 ~ 300
MinRunPctAnchor     0.01 ~ 1.00
TrailStartPctAnchor 0.01 ~ 1.00
TrailGivePctAnchor  0.01 ~ 1.00
AnchorBackPct       0.01 ~ 1.00
```

### 4. 這兩個參數固定不要動

使用者特別要求：

```text
SysHistDBars = 600
SysHistMBars = 20000
```

這兩個目前在 UI 會顯示為固定，不參與搜尋，不要改。

### 5. Top N 至少保留 3 組

之前只留 1 組會導致近 1 年測出來分歧時無法判斷。現在：

- `top_n` 預設為 3。
- 每個參數保留前幾名也至少為 3。
- 結果頁會先做 Top 3 最近 1 年驗收。

### 6. Top 3 多段驗收

已新增：

```text
Top 3 多段驗收
```

位置：

```text
結果頁 > 策略組合 tab
```

按下「計算 Top 3 多段驗收」後，系統會：

- 把 Top 3 候選分別回測多個年度 / 時間段。
- 產生熱圖。
- 顯示每段滑價淨利、報酬率、MDD、PF、交易數、判定。
- 自動排除整段都沒有交易的區段。

用途：避免只因最近 1 年剛好漂亮就上線。

### 7. 參數穩定區圖

已新增：

```text
參數穩定區圖
```

位置：

```text
結果頁 > 策略組合 tab
```

用途：

- 看 Top 候選是否集中在一片穩定區。
- 避免只有孤立尖峰。
- 可切換 X / Y 參數。
- 可用顏色觀察穩健分數、總報酬率、MDD、交易數等。

如果目前只保留 3 組，圖只能看 Top 候選分布；若要看更完整穩定區，下一輪可以暫時把保留候選調高到 10。

### 8. 參數家族等權組合

已新增：

```text
參數家族等權組合
```

用途：

- 不押單一最佳參數。
- 用 Top N 候選等權組合回測。
- 比較單一最佳 vs Top N 等權，在最佳化區、最近 1 年、全期間的差異。

如果 Top N 等權沒有改善近 1 年，代表問題可能在策略邏輯，不只是單一參數。

### 9. 舊策略 / 新策略守門

已新增：

```text
舊策略 / 新策略守門
```

位置：

```text
結果頁 > 三段比較總表後
```

邏輯：

- 每週新跑出的策略只叫「挑戰者」。
- 系統會從歷史 Top10 找一個不同參數的舊策略當 incumbent。
- 用同一個最近 1 年與全期間重新比較。
- 只有新策略明顯勝出、MDD / PF 沒明顯惡化，才建議替換。
- 否則維持舊策略、觀察、淘汰，或空手。

這是避免每週追逐雜訊的重要守門。

### 10. 過度最佳化風險分數

已新增：

```text
過度最佳化風險
```

位置：

```text
結果頁 > 決策總覽下方
```

這是 `PBO proxy`，不是完整學術版 CSCV。它目前會用以下項目估計資料探勘風險：

- 搜尋次數。
- 候選數。
- 參數穩定區。
- 平台分數。
- 多段最差窗。
- 最近 1 年落差。
- 滑價壓測。
- 審查結論。

輸出：

```text
風險分數 0 ~ 100
風險等級：低 / 中 / 高
使用建議：可進複核 / 只能觀察 / 不建議上線
```

目前測試情境曾輸出：

```text
44.68 / 100，中風險，只能觀察
```

## 目前對交易可行性的判斷

使用者目標：靠程式交易跑台指期。

目前判斷：

- 方向可以做。
- 但目前不能說能穩定賺錢。
- 也還不能說完全解決過度最佳化。
- 目前這套比較像「候選策略篩選 + 上線守門」。
- 不能直接把每週第一名重倉用在下週。

正確用法應該是：

```text
每週更新資料
每週產生候選策略
新策略只當挑戰者
舊策略預設繼續用
新策略必須通過多段驗收
新策略必須打敗舊策略
過度最佳化風險不能太高
滑價壓測後仍正報酬
不通過就維持舊策略或空手
```

## 不建議做的事

不要這樣做：

```text
每週跑一次最佳化
直接拿第一名當下週策略
每週硬換策略
看到回測漂亮就加大口數
```

原因：

- 會追逐雜訊。
- 容易 data snooping。
- 台指期槓桿高，滑價 / 跳空 / 成交差異會放大錯誤。

## 建議實盤前流程

### 第一階段：模擬盤 / forward test

至少跑：

```text
8 ~ 12 週
```

每週記錄：

- 當週候選策略。
- 系統是否建議上線。
- 實際下週績效。
- 實際滑價。
- 實際交易數。
- 是否和回測差距過大。

### 第二階段：極小資金

如果 forward test 通過，再用：

```text
微台 / 小台 / 極小口數
```

不要一開始就大台重倉。

### 第三階段：放大

只有在真實交易結果連續穩定後，再逐步放大。

## 下一步開發順序

建議接著做：

### 1. forward test 日誌

每週把系統當下判斷存檔，並在下週回填真實結果。

建議檔案：

```text
bundle\run_history\forward_test_log.csv
```

欄位建議：

```text
週別
決策日期
候選策略 signature
舊策略 signature
系統建議
過度最佳化風險分數
最近 1 年淨利
最近 1 年 MDD
最近 1 年 PF
下週實際淨利
下週實際 MDD
下週實際交易數
實際滑價
是否通過
備註
```

### 2. 真正的 PBO / CSCV

目前是 proxy。下一步可以把歷史期間切成多個區塊，做 combinatorially symmetric cross-validation。

目標：

- 看最佳化排名在樣本外是否反轉。
- 估計過度最佳化機率。

### 3. Deflated Sharpe / 多重測試懲罰

目前測很多組參數，Sharpe / 報酬應該被打折。下一步可加入 Deflated Sharpe 或類似懲罰分數。

### 4. 市場狀態過濾

策略不一定每種行情都有效。建議加入：

- 趨勢盤。
- 震盪盤。
- 高波動。
- 低波動。
- 滑價異常。
- 成交異常。

不同市場狀態下，如果策略歷史上失效，就下週停用。

### 5. 每週操作報告

最後可以做成：

```text
一鍵產生下週策略建議報告
```

報告包含：

- 本週候選。
- 舊策略比較。
- 過度最佳化風險。
- Top 3 多段驗收。
- 是否建議上線。
- 下週停用條件。

## 重要程式位置

主要 UI：

```text
mq01\ui_runtime_v2.py
```

新功能大多在這裡：

- `_overfit_risk_report`
- `_render_overfit_risk_panel`
- `_render_incumbent_challenger_panel`
- `_render_top_candidate_multiperiod_validation`
- `_render_param_stability_map`
- `_render_strategy_family_tab`

核心服務：

```text
mq01\services.py
```

常用資料：

```text
bundle\run_history\_persistent_top10_v3.json
bundle\run_history\_persistent_top10_v3.csv
bundle\run_history\_persistent_best_top1_v3.txt
```

參數 preset：

```text
bundle\param_presets\1150415.txt
```

策略 XS：

```text
bundle\strategy\1150415.xs
```

## 新機驗證命令

```cmd
cd /d C:\Users\User\Documents\mqquant\01-01
py -m py_compile app.py mq01\ui_runtime_v2.py mq01\ui_runtime_0101.py mq01\services.py mq01\job_store.py mq01\background_worker.py
```

再做 import：

```cmd
cd /d C:\Users\User\Documents\mqquant\01-01
py -c "import app; print('app_import_ok')"
```

如果單獨用 Python 測 `mq01` 模組時遇到：

```text
ModuleNotFoundError: No module named 'src'
```

通常是沒有把 `bundle` 加入 `sys.path`。Streamlit 入口和 `run.cmd` 正常情況會處理好。

## 已跑過的驗證

本輪已跑：

```cmd
py -m py_compile app.py mq01\ui_runtime_v2.py mq01\ui_runtime_0101.py mq01\services.py
```

也做過 smoke：

- 可抓到 Top 3 候選。
- 可產生參數穩定區資料。
- 可產生短區間多段驗收資料。
- 可找到不同於本次參數的舊策略。
- 過度最佳化風險分數可正常輸出。

## 接手者注意事項

1. 不要把「過度最佳化風險分數」解讀成保證獲利。
2. 不要把 `PBO proxy` 當成完整 PBO / CSCV。
3. 台指期實盤前一定要先 forward test。
4. 每週可以跑資料，但不應該每週硬換策略。
5. 新策略沒有明顯打敗舊策略，就不要換。
6. 風險高時，正確動作是空手，不是硬找另一組參數。

## 下一位 AI 的第一個任務建議

請接著做：

```text
forward test 日誌 + 每週策略決策紀錄
```

原因：

目前所有守門仍然來自歷史資料。要知道它能不能真的幫助賺錢，必須開始記錄「當週做出的決策」和「下週真實結果」。

這一步完成後，這套系統才會從回測研究工具，往實盤決策輔助工具靠近。

## 2026-04-22 Codex 追加：開發區年份 / 驗證區切分

本輪回到 `01-01` 主線，新增左側 `執行設定 > 開發區年份`。

目前行為：

- 可選 1 ~ 5 年。
- 1 年代表只用 2020。
- 2 年代表 2020~2021。
- 以此類推。
- 驗證區是剩餘資料區間，不再固定最近 1 年。
- 1 年開發 / 約 5 年驗證，2 年開發 / 約 4 年驗證，以此類推。
- 左側會顯示 `本輪開發區` 與 `驗證區` 實際日期。

本輪改動檔案：

```text
mq01/ui_runtime_v2.py
mq01/services.py
mq01/config.py
docs/01_CURRENT_STATUS.md
01_LATEST_HANDOFF_20260422.md
```

實作重點：

- `resolve_0101_research_periods()` 支援 `development_years`。
- 最佳化任務會把選到的 `development_start_date` / `development_end_date` 寫入 `runtime_settings`。
- 開發區資格審查與驗證區驗收會沿用同一份 `research_periods`，避免畫面和實際計算不一致。
- 圖表橘色分隔線改為 `驗證區起點`。
- `research_profile_tag` 已從舊的 `last1y_holdout` 切到 `01-01_dev_years_validation_v3`，避免新舊切法結果混用。
- 不同開發區年份會使用不同 `research_profile_tag`，避免歷史 Top10 / incumbent 混用不同開發區結果。

已驗證：

```cmd
py -m py_compile app.py mq01\ui_runtime_v2.py mq01\services.py mq01\config.py
py -c "import app; print('app_import_ok')"
```

注意：

- 尚未啟動長掛 Streamlit 實機點選驗證。
- 選不同開發區年份後，需要重新跑最佳化，該年份 profile 才會有自己的 Top10 / 歷史最佳。

---

# LATEST_HANDOFF

## 本次任務
- 回到 `01-01` 專案，補上 MDD 批次門檻測試。
- 使用者指出目前只有單一 `最大 MDD(%)`，無法輸入 3% ~ 15% 這種批次參數測試。

## 本次完成
- 左側 `硬性過濾` 新增 `MDD 門檻模式`：
  - `固定門檻`：保留原本單一 `最大 MDD(%)`。
  - `範圍測試`：可輸入 `MDD 起點(%)`、`MDD 終點(%)`、`MDD 步長(%)`。
- MDD 範圍測試仍維持硬過濾邏輯：
  - 以範圍上限作為實際淘汰門檻。
  - 同一批策略回測完成後，再依各 MDD 門檻分層統計通過組數與最佳表現。
- 執行中與執行後畫面新增 `MDD 門檻批次結果` 表。
- 最佳化保留候選數會依 MDD 門檻數擴大，避免低 MDD 門檻候選過早被單一總排名裁掉。

## 修改檔案
- `mq01/ui_runtime_v2.py`
- `mq01/services.py`
- `mq01/config.py`
- `mq01/background_worker.py`
- `docs/01_CURRENT_STATUS.md`
- `01_LATEST_HANDOFF_20260422.md`

## 新增檔案
- 無

## 目前狀態
- `01-01` 左側已可選 `MDD 門檻模式 = 範圍測試`。
- 範圍測試預設為 3% ~ 15%，步長 1%。
- 背景任務 state 在 MDD 範圍測試時會保留較多 top rows，供畫面顯示門檻分層。

## 已知問題
- 尚未由瀏覽器實際點選 Streamlit UI 驗證畫面位置與互動。
- 目前資料夾不是 Git repo，無法用 `git diff/status` 確認版本差異。

## 風險提醒
- MDD 範圍測試不會把每個 MDD 門檻重新跑一次；它是同一批策略結果跑完後做門檻分層。
- 若使用者把步長設得非常小，候選保留量會增加，但目前上限限制為 500 筆，避免 state 過大。

## 建議下一步
- 使用瀏覽器重新整理 `localhost:8501`，確認左側 `硬性過濾` 是否出現 `MDD 門檻模式`。
- 選 `範圍測試`，設定例如 3 / 15 / 1 後跑一次短測。
- 檢查執行中與完成後是否出現 `MDD 門檻批次結果`。

## 本次結果檔
- 無新增結果檔；本次是功能修正與文件回寫。

## 已驗證
- `py -m py_compile app.py mq01\ui_runtime_v2.py mq01\services.py mq01\config.py mq01\background_worker.py mq01\job_store.py`
- `py -c "import app; print('app_import_ok')"`
- MDD sweep helper 小測：3 / 6 / 9 門檻可正確產生分層表。

---

# LATEST_HANDOFF 2026-04-23

## 本次任務
- 在 `01-01-01` 工作副本新增第一個實作模組：多輪 WFO（Walk-Forward）骨架。
- 本輪只做 WFO 骨架，不做 gap、PBO / DSR、Forward Test，也不做最終決策整合。

## 本次完成
- 新增 WFO 切窗工具，可設定：
  - 訓練窗長度
  - 測試窗長度
  - 滑動步長
- 單次最佳化完成後，右側新增 `多輪 WFO 驗證` 區塊。
- 左側 `執行設定` 新增：
  - `WFO 訓練窗(年)`
  - `WFO 測試窗(年)`
  - `WFO 滑動步長(年)`
- 按 `計算多輪 WFO` 後，每輪會：
  - 用該輪訓練窗重新跑最佳化。
  - 保留前 N 名候選。
  - 對下一段測試窗做 OOS 驗收。
  - 輸出 OOS 報酬、OOS 最大回撤、OOS 交易數與是否通過最低門檻。
- 最後輸出 WFO 總表：
  - 總輪數
  - 通過輪數
  - 失敗輪數
  - 平均 OOS 報酬
  - 平均 OOS 最大回撤
  - 最差一輪表現

## 修改檔案
- `mq01/config.py`
- `mq01/services.py`
- `mq01/ui_runtime_v2.py`
- `docs/01_CURRENT_STATUS.md`
- `01_LATEST_HANDOFF_20260422.md`

## 新增檔案
- 無

## WFO 流程說明
- WFO 起點使用目前研究資料的 `development_start_date`。
- WFO 終點使用目前研究資料的 `holdout_end_date`。
- 每輪流程為：
  1. 依訓練窗切出 train period。
  2. 在 train period 內用現有最佳化器重新搜尋候選。
  3. 取該輪 Top N 候選。
  4. 用同一組參數在下一段 test period 做 OOS 回測。
  5. 將第一名候選寫入 fold summary，並保留 Top N 明細。
- 本輪沒有 gap，測試窗直接接在訓練窗後方。

## 目前可跑到哪裡
- `run.cmd` 啟動後，先照原流程跑一次單次最佳化。
- 單次最佳化完成、有最佳候選後，右側會出現 `多輪 WFO 驗證`。
- 按 `計算多輪 WFO` 可以產生 WFO 總表與各輪 Top N 明細。

## 已知限制
- WFO 是同步計算，按下後會等待每一輪訓練窗重新最佳化完成，時間會比單次驗證長。
- 本輪尚未加入 gap。
- 本輪尚未加入 PBO / DSR。
- 本輪尚未加入 Forward Test 日誌。
- 本輪尚未整合最終決策引擎。
- Top N 穩定區與參數家族目前仍沿用既有結果頁功能，尚未提升到 WFO fold 層級。

## 已驗證
- `py -m py_compile app.py mq01\ui_runtime_v2.py mq01\services.py mq01\config.py mq01\background_worker.py mq01\job_store.py`
- `py -c "import app; print('app_import_ok')"`
- WFO 切窗 smoke：4 年訓練 / 1 年測試 / 1 年步長可切出 2 輪。
- 固定參數 WFO smoke：可完成 2 輪並輸出 fold summary。

## 下一輪建議
- 做第二個模組：`gap + 穩定區 / 參數家族分析`。
- 建議把 gap 加到 WFO 切窗層，並將既有 `plateau_score` / `參數家族等權組合` 擴充成 WFO fold 層級的穩定區摘要。

---

# LATEST_HANDOFF 2026-04-23-2

## 本次任務
- 使用者澄清目標不是只有 WFO 骨架，而是要把 `01-01-01` 升級成「多層策略驗證與決策系統」。
- 核心目的：降低假冠軍與過度最佳化風險，不再只看總回測第一名。

## 本次完成
- WFO 新增 `gap 天數`。
- WFO 每輪輸出補上：
  - gap 區間
  - OOS Sharpe
  - 最差一輪 MDD
  - OOS 穩定度分數
  - 通過率
- 單次 OOS 區塊明確標示：只能初篩，不能單獨決定上線。
- 滑價壓力測試改為 2 / 3 / 4 / 5 點，並輸出 Sharpe 與是否正報酬。
- 新增 `多層策略驗證與決策系統` 面板，列出：
  - 第 1 層：單次 OOS
  - 第 2 層：多輪 WFO
  - 第 3 層：gap / purge 防污染
  - 第 4 層：PBO / DSR
  - 第 5 層：Forward Test
  - 滑價壓測
  - 穩定區 / 尖峰警示
- 新增 PBO / DSR 可落地近似版：
  - PBO 由既有過度最佳化風險分數與 WFO 失敗率折算。
  - DSR 由 Sharpe 扣除搜尋次數與候選數懲罰。
  - UI 顯示低 / 中 / 高。
- 新增 Forward Test 前測池 UI，可寫入：
  - `bundle/run_history/forward_test_log.csv`
- 最終建議目前會輸出四種之一：
  - 維持 Baseline
  - 讓 Challenger 進前測
  - 讓 Challenger 升格
  - 暫停上線，繼續研究

## 修改檔案
- `mq01/config.py`
- `mq01/services.py`
- `mq01/ui_runtime_v2.py`
- `docs/01_CURRENT_STATUS.md`
- `01_LATEST_HANDOFF_20260422.md`

## 新增檔案
- `bundle/run_history/forward_test_log.csv`

## 目前可跑到哪裡
- 跑完單次最佳化後，結果頁上方會看到原本決策總覽。
- 接著會看到 `多輪 WFO 驗證`，可設定訓練窗 / 測試窗 / 步長 / gap。
- 再下方會看到 `多層策略驗證與決策系統`，它會整合單次 OOS、WFO、gap、PBO / DSR、滑價壓測與 Forward Test 狀態。
- 按 `驗證區驗收` 後，滑價壓測會顯示 2 / 3 / 4 / 5 點。

## PBO 定義與近似做法
- 目前不是完整 CSCV PBO。
- 近似做法：將既有過度最佳化風險分數轉為 0 ~ 1，再與 WFO 失敗率加權。
- 判定：
  - <= 0.25：低
  - <= 0.55：中
  - > 0.55：高

## DSR 定義與近似做法
- 目前不是完整 Deflated Sharpe。
- 近似做法：用單次 OOS Sharpe，扣除搜尋次數與候選數懲罰；若已有 WFO，會與 WFO 平均 OOS Sharpe 平均。
- 判定：
  - >= 1.0：高
  - >= 0.3：中
  - < 0.3：低

## 已知限制
- PBO / DSR 是近似版，不是完整學術版。
- Forward Test 目前先建立資料流與 CSV 紀錄，實際下週績效仍需人工回填。
- 「升格正式策略」目前需要 Forward Test 紀錄支持，UI 已可記錄，但尚未做自動讀取歷史前測結論來升格。
- purge / embargo 目前先做基本 gap，尚未做完整事件標籤型 purge。

## 已驗證
- `py -m py_compile app.py mq01\ui_runtime_v2.py mq01\services.py mq01\config.py mq01\background_worker.py mq01\job_store.py`
- `py -c "import app; print('app_import_ok')"`
- WFO smoke：4 年訓練 / 1 年測試 / 1 年步長 / gap 5 天可完成 2 輪。
- 滑價 smoke：驗證區滑價結果包含 2 / 3 / 4 / 5 點，且每列有 Sharpe。

## 下一輪建議
- 將 PBO 擴充為更正式的 CSCV / rank-logit 版本。
- 將 Forward Test log 的歷史結果讀回 UI，自動判斷 Challenger 是否已通過前測並允許升格。
- 將穩定區分析提升到 WFO fold 層級，而不是只看單次最佳化 Top N。
