# MQQuant 01-01

`01-01` 是從 `01` 複製出來的獨立工作線，目前只改 `01-01` 自己，不碰以下既有專案：

- `xs-core-engine`
- `01`
- `02`

## 目前定位

- UI 方向：單頁 `策略研究開發工作台`，左側設定、右側監控與結果。
- 研究方向：開發區最佳化、最近 1 年驗收、全期間比較、KPI 與逐筆交易整合在同一頁。
- 目前入口：`app.py` 會走 `mq01.ui_runtime_v2`，不是 `01` 原本那個 runtime。

## 先看哪裡

換電腦或接手時，請先看這幾份：

1. [00_START_HERE_換電腦.md](C:\Users\User\Documents\mqquant\01-01\00_START_HERE_換電腦.md)
2. [docs/01_CURRENT_STATUS.md](C:\Users\User\Documents\mqquant\01-01\docs\01_CURRENT_STATUS.md)
3. [docs/02_FOR_NEXT_AGENT.md](C:\Users\User\Documents\mqquant\01-01\docs\02_FOR_NEXT_AGENT.md)
4. `01-01-01 專案完整流程與概念說明.md`

## 啟動方式

```cmd
cd /d C:\Users\User\Documents\mqquant\01-01
run.cmd
```

`run.cmd` 會：

- 切到 `01-01` 目錄
- 如果沒設定 `MQQUANT_SOURCE_ROOT`，自動用 `01-01\bundle`
- 用 `streamlit` 啟動 `app.py`

## 目前重要檔案

- [app.py](C:\Users\User\Documents\mqquant\01-01\app.py)
- [run.cmd](C:\Users\User\Documents\mqquant\01-01\run.cmd)
- [mq01/ui_runtime_v2.py](C:\Users\User\Documents\mqquant\01-01\mq01\ui_runtime_v2.py)
- [mq01/services.py](C:\Users\User\Documents\mqquant\01-01\mq01\services.py)
- [mq01/ui_runtime_0101.py](C:\Users\User\Documents\mqquant\01-01\mq01\ui_runtime_0101.py)
- [mq01/job_store.py](C:\Users\User\Documents\mqquant\01-01\mq01\job_store.py)
- [mq01/background_worker.py](C:\Users\User\Documents\mqquant\01-01\mq01\background_worker.py)

## 目前已做的控制層改善

- `01-01` 已切到 `ui_runtime_v2` 單頁工作台。
- 未執行時會顯示連續式使用說明；執行中會隱藏說明。
- 按開始後，`即時執行監控` 會移到右側最上方。
- 每次最佳策略落盤後，會自動產生最新 `strategy\<命名>.xs` 與 `param_presets\<命名>.txt`，並回填左側路徑。
- 控制頁已加入 Python worker 監控與手動清理能力。

## 注意

- 不要直接去改 `01` 來帶動 `01-01`。
- 不要用長掛式 `streamlit run ...` 驗證來卡住工作流程。
- 驗證以短命令為主，例如：
  - `py -m py_compile ...`
  - `py -c "import app"`
