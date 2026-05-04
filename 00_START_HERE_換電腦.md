# 01-01 移機交接入口

這份就是新機第一個要讀的檔。

如果你是要交給下一位 AI / Codex / 工程師，請直接先叫他讀：

1. `C:\Users\User\Documents\mqquant\01-01\00_START_HERE_換電腦.md`
2. `C:\Users\User\Documents\mqquant\01-01\docs\01_CURRENT_STATUS.md`
3. `C:\Users\User\Documents\mqquant\01-01\docs\02_FOR_NEXT_AGENT.md`

## 這個專案現在是什麼

- 專案名稱：`01-01`
- 目前入口：`app.py`
- 目前 UI runtime：`mq01/ui_runtime_v2.py`
- 啟動方式：`run.cmd`
- `run.cmd` 會自動把 `MQQUANT_SOURCE_ROOT` 指到 `01-01\bundle`，再用 Streamlit 啟動 `app.py`

也就是說，新機只要把整個 `01-01` 資料夾搬過去，先確保 Python / Streamlit 可跑，再從 `run.cmd` 進去即可。

## 新機先做什麼

1. 把整個資料夾搬到新機。
2. 確認 Python 可用。
3. 進入：

```cmd
cd /d C:\Users\User\Documents\mqquant\01-01
run.cmd
```

4. 如果資料路徑和舊機不同，進 Streamlit 後先改左側的：
   - `XS 路徑`
   - `M1 路徑`
   - `D1 路徑`
   - `參數範圍 preset`

## 目前 01-01 v2 的定位

現在不是三分頁，而是單頁研究流程。

左側：
- 路徑
- 搜尋模式
- 執行設定
- 硬性過濾

右側：
- 最上方先看執行中監控
- 中間看最佳化區即時摘要 / 完成摘要
- 下方看上線資格審查
- 再往下看最佳化區、最近 1 年、全期間比較、圖表、KPI、逐筆交易

## 最近已完成的重要修正

### 1. heartbeat 不再自動停工

- 背景程序不會再因 heartbeat timeout 自動停掉。
- 現在只會標記 stale / 殘留，是否清理由 UI 手動控制。

### 2. 執行中會自動刷新

- 執行中頁面已補回自動刷新。
- 背景程序還在跑時，頁面會定期刷新進度與目前最佳候選。

### 3. 執行中最上方已補回監控卡

目前上方監控重點包含：

- CPU 即時使用率
- 記憶體即時使用率
- 背景程序負載
- 平均測試組數 / 每分鐘
- 平均通過組數 / 每分鐘
- 平均每組秒數
- 配置背景程序數
- 可用 CPU 核心數
- 目前步驟

另外「背景程序監控」也已移到最上面。

### 4. 執行中就會顯示最佳化區

- 不用等全部跑完。
- 只要當前已有最佳候選，右側就會即時顯示最佳化區摘要。

### 5. 圖表已改成上下排列

以下區塊都改成：

- 先資產曲線
- 再每週損益

不再左右並排。

### 6. 最近 1 年「沒反應」的主因已修掉

之前有兩條不同資料路徑：

- 一條是「最近 1 年快照」
- 一條是「最近 1 年驗收」

它們用的回測方式不一致，所以會出現：

- 按了按鈕後很多欄位變成 `--`
- 圖像像沒資料
- 交易數看起來對不上

現在已統一成：

- 先跑全期間回測
- 再依指定期間過濾到最近 1 年

所以最近 1 年快照、驗收、圖表、KPI 會走同一條資料路徑。

### 7. 零交易時不再直接炸掉

之前最近 1 年如果剛好 0 筆交易，KPI 會因為回傳格式不對直接報錯。

這點也已修掉，現在 0 筆交易時會穩定回傳空 KPI，而不是整頁 crash。

### 8. 首頁說明改成連續式操作手冊

- 未執行時，首頁會顯示完整連續式說明。
- 說明不再用分頁。
- 最上方先用四步驟說明整體主線：設定、最佳化、驗收、總結。

### 9. 執行中監控置頂

- 按開始後，右側不再顯示使用說明。
- `即時執行監控` 會成為右側最上方內容。
- 停止 / 下載操作列移到監控區下方，避免擋在最前面。

### 10. 最新 XS / preset 自動回填

- 每次最佳策略落盤後，會另外產生：
  - `bundle\strategy\<命名>.xs`
  - `bundle\param_presets\<命名>.txt`
- 左側 `XS 路徑` 和 `參數範圍 preset` 會自動切到最新檔。
- 命名規則：`民國年3碼 + 月日 + 時分 + 總報酬率整數`。
- 範例：2026-04-21 08:20，總報酬率 133%，檔名為 `11504210820133`。

## 目前畫面應該長什麼樣

你在新機驗證時，請先檢查這幾點：

### A. 執行中

- 標題應該是：`策略研究開發工作台`
- 開始執行後，使用說明應該消失
- 右側最上方先看到 `即時執行監控`
- 下面有 `檢視背景程序監控`
- 再下面才是進度、最佳化區即時摘要、目前最佳候選

### B. 跑完最佳化後

- 會出現最佳化區結果
- 會出現上線資格審查
- 會出現最佳化區理論 / 滑價摘要
- 會出現往回推年度報酬
- 會出現垂直排列的圖表

### C. 按「測試最近 1 年」後

- 要出現最近 1 年審查摘要
- 要出現三段比較總表
- 要出現最近 1 年圖表
- 要出現全期間圖表
- 要出現完整 KPI 對照

如果按了近 1 年後只有 `--`、空圖、或完全沒變化，就代表新機還有資料路徑或環境差異要查。

## 新機驗證命令

先做語法檢查：

```cmd
cd /d C:\Users\User\Documents\mqquant\01-01
py -m py_compile app.py mq01\ui_runtime_v2.py mq01\ui_runtime_0101.py mq01\services.py mq01\job_store.py mq01\background_worker.py bundle\src\backtest\report.py
```

再做 import 檢查：

```cmd
cd /d C:\Users\User\Documents\mqquant\01-01
py -c "import app; print('app_import_ok')"
```

## 新機如果要交給 AI，請他優先讀哪些程式

如果只是要理解入口與主畫面，先讀：

1. `C:\Users\User\Documents\mqquant\01-01\app.py`
2. `C:\Users\User\Documents\mqquant\01-01\mq01\ui_runtime_v2.py`

如果要查最近 1 年驗收 / 區間分析邏輯，再讀：

3. `C:\Users\User\Documents\mqquant\01-01\mq01\services.py`
4. `C:\Users\User\Documents\mqquant\01-01\bundle\src\backtest\report.py`

如果要查背景程序監控，再讀：

5. `C:\Users\User\Documents\mqquant\01-01\mq01\ui_runtime_0101.py`
6. `C:\Users\User\Documents\mqquant\01-01\mq01\background_worker.py`
7. `C:\Users\User\Documents\mqquant\01-01\mq01\job_store.py`

## 一句話版交接

新機先讀 `00_START_HERE_換電腦.md`，再跑 `run.cmd`；如果 UI 有起來，就先驗證「執行中監控卡會更新」和「按測試最近 1 年後真的會出現近 1 年摘要 / 圖表 / KPI」，這是目前最重要的兩個檢查點。
