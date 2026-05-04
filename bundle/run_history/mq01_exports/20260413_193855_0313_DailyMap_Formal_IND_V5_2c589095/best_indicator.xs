//=======================================================================
// ScriptName : 0313_DailyMap_Formal_IND_V5
// 說明       : 0313 V5 高頻版（指標版，多頻率顯示 + TXT輸出）
// 核心模型   : 日K定錨 + NH/NL 或 Don確認 + ATR濾網 + 多層出場引擎 + 1分K Open執行
//
// 固定骨架：
// 1. BeginTime      = 084800
// 2. EndTime        = 124000
// 3. ForceExitTime  = 131200
// 4. 日K Bias 固定 MA = 3 / 5、EMA = 3 / 5
//
// V5 變更：
// 1. 進場由「雙重最嚴」改為「NH/NL 或 Don 任一成立」
// 2. 下修 EntryBufferPts / DonBufferPts 預設值
// 3. Bias 由嚴格 AND 改為較寬鬆 OR
// 4. 單日最多進場次數仍固定維持可測，但建議維持 2
//
// 出場引擎：
// 1. ATR 固定停損
// 2. ATR 固定停利
// 3. 時間停損（定錨%）
// 4. 回吐停利（定錨%）
// 5. 08:48 定錨失敗出場（定錨%）
// 6. 強制平倉
//
// 規範：
// 1. 進場 / 出場 一律當根 Open 判斷、當根 Open 執行
// 2. 不使用當根 Close/High/Low 作為交易判斷
// 3. 回吐停利追蹤僅使用前一根已完成K棒 High[1] / Low[1]
// 4. TXT 第一行輸出參數列，後續輸出交易事件
//=======================================================================

//====================== C1.參數區 ======================
input:
    DonLen               (1,      "1.Don長度"),
    ATRLen               (273,      "2.ATR長度"),
    EMAWarmBars          (1,      "3.EMA定錨回推日數"),

    EntryBufferPts       (72,     "4.NH/NL突破緩衝點數"),
    DonBufferPts         (255,     "5.Don突破緩衝點數"),
    MinATRD              (189,      "6.最小日ATR濾網"),
    ATRStopK             (1.41,   "7.ATR停損倍數"),
    ATRTakeProfitK       (1.09,   "8.ATR停利倍率"),

    MaxEntriesPerDay     (10,      "9.單日最多進場次數"),
    TimeStopBars         (46,     "10.時間停損Bars"),

    MinRunPctAnchor      (0.24,   "11.時間停損最小發動(定錨%)"),
    TrailStartPctAnchor  (0.88,   "12.回吐停利啟動(定錨%)"),
    TrailGivePctAnchor   (0.04,   "13.回吐停利允許回吐(定錨%)"),

    UseAnchorExit        (1,      "14.是否啟用08:48定錨失敗出場(1=是,0=否)"),
    AnchorBackPct        (0.9,   "15.定錨失敗出場(定錨%)");

//====================== C2.變數區 ======================
var:
    isMinChart(false),

    fixedBeginTime(084800),
    fixedEndTime(124000),
    fixedForceExitTime(131200),

    fixedMALen2(3),
    fixedMALen3(5),
    fixedEMALen2(3),
    fixedEMALen3(5),

    sessOnEntry(0),
    sessOnManage(0),
    warmupBars(0),

    yH(0),
    yL(0),
    yC(0),

    ma2D(0),
    ma3D(0),

    ema2D(0),
    ema3D(0),

    alpha2(0),
    alpha3(0),

    donHiD(0),
    donLoD(0),
    atrD(0),

    cdpVal(0),
    nhVal(0),
    nlVal(0),

    LongBias(false),
    ShortBias(false),

    LongEntrySig(false),
    ShortEntrySig(false),
    LongExitTrig(false),
    ShortExitTrig(false),
    ForceExitTrig(false),

    posFlag(0),              // 0=無部位, 1=多單, -1=空單
    cost(0),
    entryATRD(0),
    dayEntryCount(0),
    entryBarNo(0),

    bestHighSinceEntry(0),
    bestLowSinceEntry(0),
    maxRunUpPts(0),
    maxRunDnPts(0),
    barsHeld(0),

    dayAnchorOpen(0),

    minRunPtsByAnchor(0),
    trailStartPtsByAnchor(0),
    trailGivePtsByAnchor(0),
    anchorBackPtsByAnchor(0),

    lastMarkBar(-9999),

    i(0),
    maSum(0),
    tmpHi(0),
    tmpLo(0),
    tmpTR(0),
    atrSum(0),

    longMark(0),
    shortMark(0),
    longExitMark(0),
    shortExitMark(0),
    forceExitMark(0),

    fpath(""),
    hdrPrinted(false),
    outStr(""),
    hh(0),
    mm(0),
    ss(0),
    timeStr(""),
    dateTimeStr(""),
    hasTradeEvent(false),

    longEntryLevelNH(0),
    longEntryLevelDon(0),
    shortEntryLevelNL(0),
    shortEntryLevelDon(0),

    atrStopLong(0),
    atrStopShort(0),
    atrTPPriceLong(0),
    atrTPPriceShort(0),

    LongEntryReady(false),
    ShortEntryReady(false),

    LongExitByATR(false),
    ShortExitByATR(false),
    LongExitByTP(false),
    ShortExitByTP(false),
    LongExitByTime(false),
    ShortExitByTime(false),
    LongExitByTrail(false),
    ShortExitByTrail(false),
    LongExitByAnchor(false),
    ShortExitByAnchor(false);

//====================== C3.基本檢查 ======================
isMinChart = (BarFreq = "Min");

if DonLen < 1 then
    RaiseRunTimeError("DonLen 必須 >= 1");
if ATRLen < 1 then
    RaiseRunTimeError("ATRLen 必須 >= 1");
if EMAWarmBars < 1 then
    RaiseRunTimeError("EMAWarmBars 必須 >= 1");
if ATRTakeProfitK <= 0 then
    RaiseRunTimeError("ATRTakeProfitK 必須 > 0");

//====================== C4.分鐘線專用控制 ======================
if isMinChart then begin
    sessOnEntry  = IFF((Time >= fixedBeginTime) and (Time <= fixedEndTime), 1, 0);
    sessOnManage = IFF((Time >= fixedBeginTime) and (Time <= fixedForceExitTime), 1, 0);
end
else begin
    sessOnEntry  = 0;
    sessOnManage = 0;
end;

warmupBars = MaxList(fixedMALen3 + 2, fixedEMALen3 + 2, DonLen + 2, ATRLen + 2, EMAWarmBars + 2);

//====================== C5.初始化 ======================
if CurrentBar = 1 then begin
    posFlag = 0;
    cost = 0;
    entryATRD = 0;
    dayEntryCount = 0;
    entryBarNo = 0;

    bestHighSinceEntry = 0;
    bestLowSinceEntry = 0;
    maxRunUpPts = 0;
    maxRunDnPts = 0;
    barsHeld = 0;

    dayAnchorOpen = 0;
    minRunPtsByAnchor = 0;
    trailStartPtsByAnchor = 0;
    trailGivePtsByAnchor = 0;
    anchorBackPtsByAnchor = 0;

    lastMarkBar = -9999;

    hdrPrinted = false;
    fpath = "C:\\XQ\\data\\" + "[ScriptName]_[Date]_[StartTime].txt";
end;

//====================== C6.每日重置 + 日K定錨 ======================
if Date <> Date[1] then begin

    yH = GetField("最高價", "D")[1];
    yL = GetField("最低價", "D")[1];
    yC = GetField("收盤價", "D")[1];

    //================ 日K定錨 MA2（固定3） =================
    maSum = 0;
    for i = 1 to fixedMALen2 begin
        maSum = maSum + GetField("收盤價", "D")[i];
    end;
    ma2D = maSum / fixedMALen2;

    //================ 日K定錨 MA3（固定5） =================
    maSum = 0;
    for i = 1 to fixedMALen3 begin
        maSum = maSum + GetField("收盤價", "D")[i];
    end;
    ma3D = maSum / fixedMALen3;

    //================ EMA alpha（固定3 / 5） =================
    alpha2 = 2.0 / (fixedEMALen2 + 1);
    alpha3 = 2.0 / (fixedEMALen3 + 1);

    //================ 日K定錨 EMA2（固定3） =================
    ema2D = GetField("收盤價", "D")[EMAWarmBars];
    for i = EMAWarmBars - 1 downto 1 begin
        ema2D = alpha2 * GetField("收盤價", "D")[i] + (1 - alpha2) * ema2D;
    end;

    //================ 日K定錨 EMA3（固定5） =================
    ema3D = GetField("收盤價", "D")[EMAWarmBars];
    for i = EMAWarmBars - 1 downto 1 begin
        ema3D = alpha3 * GetField("收盤價", "D")[i] + (1 - alpha3) * ema3D;
    end;

    //================ Donchian（日K定錨） =================
    tmpHi = GetField("最高價", "D")[1];
    tmpLo = GetField("最低價", "D")[1];

    for i = 2 to DonLen begin
        if GetField("最高價", "D")[i] > tmpHi then
            tmpHi = GetField("最高價", "D")[i];

        if GetField("最低價", "D")[i] < tmpLo then
            tmpLo = GetField("最低價", "D")[i];
    end;

    donHiD = tmpHi;
    donLoD = tmpLo;

    //================ ATR（日K定錨，簡單平均） =================
    atrSum = 0;
    for i = 1 to ATRLen begin
        tmpTR = MaxList(
                    GetField("最高價", "D")[i] - GetField("最低價", "D")[i],
                    AbsValue(GetField("最高價", "D")[i] - GetField("收盤價", "D")[i + 1]),
                    AbsValue(GetField("最低價", "D")[i] - GetField("收盤價", "D")[i + 1])
                );
        atrSum = atrSum + tmpTR;
    end;
    atrD = atrSum / ATRLen;

    //================ CDP / NH / NL =================
    cdpVal = (yH + yL + 2 * yC) / 4;
    nhVal  = 2 * cdpVal - yL;
    nlVal  = 2 * cdpVal - yH;

    //================ Bias（V5：放寬） =================
    LongBias  = false;
    ShortBias = false;

    if ((ma2D > ma3D) or (ema2D > ema3D)) and (yC > cdpVal) then
        LongBias = true;

    if ((ma2D < ma3D) or (ema2D < ema3D)) and (yC < cdpVal) then
        ShortBias = true;

    //================ 分鐘線才重置盤中狀態 =================
    if isMinChart then begin
        posFlag = 0;
        cost = 0;
        entryATRD = 0;
        dayEntryCount = 0;
        entryBarNo = 0;

        bestHighSinceEntry = 0;
        bestLowSinceEntry = 0;
        maxRunUpPts = 0;
        maxRunDnPts = 0;
        barsHeld = 0;

        dayAnchorOpen = 0;
        minRunPtsByAnchor = 0;
        trailStartPtsByAnchor = 0;
        trailGivePtsByAnchor = 0;
        anchorBackPtsByAnchor = 0;

        lastMarkBar = -9999;
    end;
end;

//====================== C7.08:48定錨價與百分比換算 ======================
if isMinChart then begin
    if (Time = fixedBeginTime) and (dayAnchorOpen = 0) then
        dayAnchorOpen = Open;

    if dayAnchorOpen > 0 then begin
        minRunPtsByAnchor     = dayAnchorOpen * MinRunPctAnchor * 0.01;
        trailStartPtsByAnchor = dayAnchorOpen * TrailStartPctAnchor * 0.01;
        trailGivePtsByAnchor  = dayAnchorOpen * TrailGivePctAnchor * 0.01;
        anchorBackPtsByAnchor = dayAnchorOpen * AnchorBackPct * 0.01;
    end
    else begin
        minRunPtsByAnchor     = 0;
        trailStartPtsByAnchor = 0;
        trailGivePtsByAnchor  = 0;
        anchorBackPtsByAnchor = 0;
    end;
end;

//====================== C8.訊號預設重置 ======================
LongEntrySig      = false;
ShortEntrySig     = false;
LongExitTrig      = false;
ShortExitTrig     = false;
ForceExitTrig     = false;

LongEntryReady    = false;
ShortEntryReady   = false;

LongExitByATR     = false;
ShortExitByATR    = false;
LongExitByTP      = false;
ShortExitByTP     = false;
LongExitByTime    = false;
ShortExitByTime   = false;
LongExitByTrail   = false;
ShortExitByTrail  = false;
LongExitByAnchor  = false;
ShortExitByAnchor = false;

//====================== C9.只有分鐘線才計算盤中進出場 ======================
if isMinChart then begin

    //================ 進場門檻（V5：NH/NL 或 Don 任一成立） =================
    longEntryLevelNH   = nhVal + EntryBufferPts;
    longEntryLevelDon  = donHiD + DonBufferPts;
    shortEntryLevelNL  = nlVal - EntryBufferPts;
    shortEntryLevelDon = donLoD - DonBufferPts;

    //================ 更新持倉統計（只用前一根已完成K棒） =================
    if (posFlag <> 0) and (CurrentBar > entryBarNo) then begin

        if posFlag = 1 then begin
            if High[1] > bestHighSinceEntry then
                bestHighSinceEntry = High[1];
            if Low[1] < bestLowSinceEntry then
                bestLowSinceEntry = Low[1];

            maxRunUpPts = bestHighSinceEntry - cost;
            maxRunDnPts = cost - bestLowSinceEntry;
        end;

        if posFlag = -1 then begin
            if Low[1] < bestLowSinceEntry then
                bestLowSinceEntry = Low[1];
            if High[1] > bestHighSinceEntry then
                bestHighSinceEntry = High[1];

            maxRunUpPts = cost - bestLowSinceEntry;
            maxRunDnPts = bestHighSinceEntry - cost;
        end;
    end;

    if posFlag <> 0 then
        barsHeld = CurrentBar - entryBarNo
    else
        barsHeld = 0;

    //================ 初始 ATR 停損 / 停利 =================
    atrStopLong    = cost - ATRStopK * entryATRD;
    atrStopShort   = cost + ATRStopK * entryATRD;
    atrTPPriceLong = cost + ATRTakeProfitK * entryATRD;
    atrTPPriceShort = cost - ATRTakeProfitK * entryATRD;

    //================ 出場優先 =================
    if (sessOnManage = 1) and (CurrentBar > warmupBars) and (lastMarkBar <> CurrentBar) then begin

        if (Time >= fixedForceExitTime) and (posFlag <> 0) then begin
            ForceExitTrig = true;
        end
        else begin

            if posFlag = 1 then begin

                LongExitByATR = (entryATRD > 0) and (Open <= atrStopLong);
                LongExitByTP  = (entryATRD > 0) and (Open >= atrTPPriceLong);
                LongExitByTime = (barsHeld >= TimeStopBars) and (maxRunUpPts < minRunPtsByAnchor);
                LongExitByTrail = (maxRunUpPts >= trailStartPtsByAnchor) and ((bestHighSinceEntry - Open) >= trailGivePtsByAnchor);
                LongExitByAnchor = (UseAnchorExit = 1) and (dayAnchorOpen > 0) and (Open <= dayAnchorOpen - anchorBackPtsByAnchor);

                if LongExitByATR or LongExitByTP or LongExitByTime or LongExitByTrail or LongExitByAnchor then
                    LongExitTrig = true;
            end;

            if posFlag = -1 then begin

                ShortExitByATR = (entryATRD > 0) and (Open >= atrStopShort);
                ShortExitByTP  = (entryATRD > 0) and (Open <= atrTPPriceShort);
                ShortExitByTime = (barsHeld >= TimeStopBars) and (maxRunUpPts < minRunPtsByAnchor);
                ShortExitByTrail = (maxRunUpPts >= trailStartPtsByAnchor) and ((Open - bestLowSinceEntry) >= trailGivePtsByAnchor);
                ShortExitByAnchor = (UseAnchorExit = 1) and (dayAnchorOpen > 0) and (Open >= dayAnchorOpen + anchorBackPtsByAnchor);

                if ShortExitByATR or ShortExitByTP or ShortExitByTime or ShortExitByTrail or ShortExitByAnchor then
                    ShortExitTrig = true;
            end;

        end;
    end;

    //================ 再進場 =================
    if (sessOnEntry = 1) and (CurrentBar > warmupBars) and (lastMarkBar <> CurrentBar) then begin

        if (posFlag = 0) and (dayEntryCount < MaxEntriesPerDay) then begin

            if LongBias and (atrD >= MinATRD) and
               ((Open >= longEntryLevelNH) or (Open >= longEntryLevelDon)) then
                LongEntryReady = true;

            if ShortBias and (atrD >= MinATRD) and
               ((Open <= shortEntryLevelNL) or (Open <= shortEntryLevelDon)) then
                ShortEntryReady = true;

            if LongEntryReady then
                LongEntrySig = true
            else if ShortEntryReady then
                ShortEntrySig = true;

        end;
    end;

    //================ 狀態更新：先出場 =================
    if (sessOnManage = 1) and (CurrentBar > warmupBars) and (lastMarkBar <> CurrentBar) then begin

        if ForceExitTrig then begin
            posFlag = 0;
            cost = 0;
            entryATRD = 0;
            entryBarNo = 0;

            bestHighSinceEntry = 0;
            bestLowSinceEntry = 0;
            maxRunUpPts = 0;
            maxRunDnPts = 0;
            barsHeld = 0;

            lastMarkBar = CurrentBar;
        end
        else if LongExitTrig then begin
            posFlag = 0;
            cost = 0;
            entryATRD = 0;
            entryBarNo = 0;

            bestHighSinceEntry = 0;
            bestLowSinceEntry = 0;
            maxRunUpPts = 0;
            maxRunDnPts = 0;
            barsHeld = 0;

            lastMarkBar = CurrentBar;
        end
        else if ShortExitTrig then begin
            posFlag = 0;
            cost = 0;
            entryATRD = 0;
            entryBarNo = 0;

            bestHighSinceEntry = 0;
            bestLowSinceEntry = 0;
            maxRunUpPts = 0;
            maxRunDnPts = 0;
            barsHeld = 0;

            lastMarkBar = CurrentBar;
        end;

    end;

    //================ 狀態更新：再進場 =================
    if (sessOnEntry = 1) and (CurrentBar > warmupBars) and (lastMarkBar <> CurrentBar) then begin

        if LongEntrySig then begin
            posFlag = 1;
            cost = Open;
            entryATRD = atrD;
            dayEntryCount = dayEntryCount + 1;
            entryBarNo = CurrentBar;

            bestHighSinceEntry = Open;
            bestLowSinceEntry = Open;
            maxRunUpPts = 0;
            maxRunDnPts = 0;
            barsHeld = 0;

            lastMarkBar = CurrentBar;
        end
        else if ShortEntrySig then begin
            posFlag = -1;
            cost = Open;
            entryATRD = atrD;
            dayEntryCount = dayEntryCount + 1;
            entryBarNo = CurrentBar;

            bestHighSinceEntry = Open;
            bestLowSinceEntry = Open;
            maxRunUpPts = 0;
            maxRunDnPts = 0;
            barsHeld = 0;

            lastMarkBar = CurrentBar;
        end;

    end;

end;

//====================== C10.交易事件判斷 ======================
hasTradeEvent = false;

if LongEntrySig or ShortEntrySig or LongExitTrig or ShortExitTrig or ForceExitTrig then
    hasTradeEvent = true
else
    hasTradeEvent = false;

//====================== C11.TXT交易事件輸出（只限分鐘線） ======================
if isMinChart and hasTradeEvent then begin

    if hdrPrinted = false then begin
        outStr = "";
        outStr = outStr + "BeginTime=" + NumToStr(fixedBeginTime, 0);
        outStr = outStr + ",EndTime=" + NumToStr(fixedEndTime, 0);
        outStr = outStr + ",ForceExitTime=" + NumToStr(fixedForceExitTime, 0);
        outStr = outStr + ",FixedMA2Len=" + NumToStr(fixedMALen2, 0);
        outStr = outStr + ",FixedMA3Len=" + NumToStr(fixedMALen3, 0);
        outStr = outStr + ",FixedEMA2Len=" + NumToStr(fixedEMALen2, 0);
        outStr = outStr + ",FixedEMA3Len=" + NumToStr(fixedEMALen3, 0);
        outStr = outStr + ",DonLen=" + NumToStr(DonLen, 0);
        outStr = outStr + ",ATRLen=" + NumToStr(ATRLen, 0);
        outStr = outStr + ",EMAWarmBars=" + NumToStr(EMAWarmBars, 0);
        outStr = outStr + ",EntryBufferPts=" + NumToStr(EntryBufferPts, 0);
        outStr = outStr + ",DonBufferPts=" + NumToStr(DonBufferPts, 0);
        outStr = outStr + ",MinATRD=" + NumToStr(MinATRD, 0);
        outStr = outStr + ",ATRStopK=" + NumToStr(ATRStopK, 2);
        outStr = outStr + ",ATRTakeProfitK=" + NumToStr(ATRTakeProfitK, 2);
        outStr = outStr + ",MaxEntriesPerDay=" + NumToStr(MaxEntriesPerDay, 0);
        outStr = outStr + ",TimeStopBars=" + NumToStr(TimeStopBars, 0);
        outStr = outStr + ",MinRunPctAnchor=" + NumToStr(MinRunPctAnchor, 2);
        outStr = outStr + ",TrailStartPctAnchor=" + NumToStr(TrailStartPctAnchor, 2);
        outStr = outStr + ",TrailGivePctAnchor=" + NumToStr(TrailGivePctAnchor, 2);
        outStr = outStr + ",UseAnchorExit=" + NumToStr(UseAnchorExit, 0);
        outStr = outStr + ",AnchorBackPct=" + NumToStr(AnchorBackPct, 2);
        outStr = outStr + ",Strategy=DailyBiasSoft(MAorEMA+CDP)+NHNLorDon+ATRFilter+ATRStop+ATRTakeProfit+TimeStop+TrailExitPctAnchor+AnchorExitPctAnchor";
        print(file(fpath), outStr);
        hdrPrinted = true;
    end;

    hh = IntPortion(Time / 10000);
    mm = IntPortion((Time - hh * 10000) / 100);
    ss = Time - hh * 10000 - mm * 100;

    timeStr = "";

    if hh < 10 then
        timeStr = timeStr + "0" + NumToStr(hh, 0)
    else
        timeStr = timeStr + NumToStr(hh, 0);

    if mm < 10 then
        timeStr = timeStr + "0" + NumToStr(mm, 0)
    else
        timeStr = timeStr + NumToStr(mm, 0);

    if ss < 10 then
        timeStr = timeStr + "0" + NumToStr(ss, 0)
    else
        timeStr = timeStr + NumToStr(ss, 0);

    dateTimeStr = NumToStr(Date, 0) + timeStr;

    if LongEntrySig then begin
        outStr = dateTimeStr + " " + NumToStr(IntPortion(Open), 0) + " 新買";
        print(file(fpath), outStr);
    end
    else if ShortEntrySig then begin
        outStr = dateTimeStr + " " + NumToStr(IntPortion(Open), 0) + " 新賣";
        print(file(fpath), outStr);
    end
    else if LongExitTrig then begin
        outStr = dateTimeStr + " " + NumToStr(IntPortion(Open), 0) + " 平賣";
        print(file(fpath), outStr);
    end
    else if ShortExitTrig then begin
        outStr = dateTimeStr + " " + NumToStr(IntPortion(Open), 0) + " 平買";
        print(file(fpath), outStr);
    end
    else if ForceExitTrig then begin
        outStr = dateTimeStr + " " + NumToStr(IntPortion(Open), 0) + " 強制平倉";
        print(file(fpath), outStr);
    end;
end;

//====================== C12.顯示用輔助值 ======================
if isMinChart then begin
    longMark      = IFF(LongEntrySig,  Open, 0);
    shortMark     = IFF(ShortEntrySig, Open, 0);
    longExitMark  = IFF(LongExitTrig,  Open, 0);
    shortExitMark = IFF(ShortExitTrig, Open, 0);
    forceExitMark = IFF(ForceExitTrig, Open, 0);
end
else begin
    longMark      = 0;
    shortMark     = 0;
    longExitMark  = 0;
    shortExitMark = 0;
    forceExitMark = 0;
end;

//====================== C13.Plot輸出 ======================
Plot1(longMark,      "新買");
Plot2(shortMark,     "新賣");
Plot3(longExitMark,  "平賣");
Plot4(shortExitMark, "平買");
Plot5(forceExitMark, "強制平倉");
