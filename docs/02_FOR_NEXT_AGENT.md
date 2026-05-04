# 01-01 給下一位 AI / 助手的接手說明

這份是給下一位接手 `01-01` 的 AI / Codex / 工程師直接閱讀的。

## 先理解的硬規則

不要動以下專案：

- `C:\Users\User\Documents\mqquant\xs-core-engine`
- `C:\Users\User\Documents\mqquant\01`
- `C:\Users\User\Documents\mqquant\02`

目前只能改：

- `C:\Users\User\Documents\mqquant\01-01`

## 目前真正入口

- 啟動入口：`app.py`
- 主畫面 runtime：`mq01/ui_runtime_v2.py`
- 啟動方式：`run.cmd`
- `run.cmd` 會自動把 `MQQUANT_SOURCE_ROOT` 指向 `01-01\bundle`。

## 使用者目前最在意的事

1. `01-01` 的 UI 要能讓人看得懂，尤其是第一次打開時要有完整說明。
2. 按開始後，說明要消失，`即時執行監控` 要在最上方。
3. 使用者不喜歡長時間等待卻沒進展，所以執行中要清楚顯示進度與目前最佳候選。
4. 不要再用長掛式 `streamlit run ...` 驗證。
5. 背景 Python worker 不能殘留。
6. 每次跑完最新最佳策略後，`XS 路徑` 和 `參數範圍 preset` 要自動更新到最新檔。

## 已做過的關鍵修改

### UI / 使用說明

- 頁面名稱：`策略研究開發工作台`。
- 首頁說明已改成連續式整頁說明，不再使用 tab 分頁。
- 說明區最上方有四步驟總覽：設定、最佳化、驗收、總結。
- 執行中時不顯示說明，右側優先顯示 `即時執行監控`。

### 最新策略與 preset 自動回填

- 每次最佳結果落盤時，除了原本的 export 目錄，也會另外寫：
  - `bundle\strategy\<命名>.xs`
  - `bundle\param_presets\<命名>.txt`
- UI 會讀取 latest memory，將左側：
  - `XS 路徑`
  - `參數範圍 preset`
  自動切到最新檔案。
- 命名規則：`民國年3碼 + 月日 + 時分 + 總報酬率整數`。
- 範例：2026-04-21 08:20，總報酬率 133%，命名為 `11504210820133`。

### Worker 控制

- `mq01/job_store.py`
  - heartbeat
  - stale cleanup
  - force stop
- `mq01/background_worker.py`
  - 背景執行狀態寫入
  - 完成後觸發最佳策略輸出
- `mq01/ui_runtime_0101.py`
  - 仍提供部分共用 runtime / monitor / action bar 功能，`ui_runtime_v2.py` 會引用它。

## 現在不要做的事

- 不要回頭改 `01`。
- 不要為了驗證去開長掛 Streamlit 命令。
- 不要先大改研究後端，除非使用者明確說開始改那一塊。
- 不要刪除 `bundle\run_history`，裡面有 latest memory 與歷史最佳紀錄。

## 允許的短驗證

```cmd
cd /d C:\Users\User\Documents\mqquant\01-01
py -m py_compile app.py mq01\ui_runtime_v2.py mq01\ui_runtime_0101.py mq01\services.py mq01\job_store.py mq01\background_worker.py bundle\src\backtest\report.py
py -c "import app; print('app_import_ok')"
```

## 新機接手後優先檢查

1. `run.cmd` 能不能打開頁面。
2. 頁面標題是否為 `策略研究開發工作台`。
3. 未執行時是否有完整連續式說明。
4. 執行中是否隱藏說明，並將 `即時執行監控` 放在最上方。
5. 跑完最佳化後，是否產生最新 `strategy\<命名>.xs` 與 `param_presets\<命名>.txt`。
6. 左側 `XS 路徑` 和 `參數範圍 preset` 是否自動切到最新檔。
7. `最近 1 年驗收` 是否能正常顯示摘要、圖表、KPI 與三段比較。

## 如果要繼續開發

- 主畫面與流程：`mq01/ui_runtime_v2.py`
- 最新策略輸出與命名：`mq01/services.py`
- 背景任務：`mq01/background_worker.py`
- job 狀態與 worker 控制：`mq01/job_store.py`
- 最近 1 年 / 區間分析：`mq01/services.py` 與 `bundle/src/backtest/report.py`

## 一句話版

`01-01` 現在是自足的單頁策略研究開發工作台；新機搬整個資料夾後跑 `run.cmd`，先驗證執行中監控置頂、最新 XS / preset 自動回填、最近 1 年驗收正常即可。
