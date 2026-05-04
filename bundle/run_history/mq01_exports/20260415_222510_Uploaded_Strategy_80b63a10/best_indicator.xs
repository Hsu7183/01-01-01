{*
    strategy_id: Uploaded_Strategy
    title: 最佳報酬配對
    policy_version: V2
*}

//=======================================================================
// ScriptName : Uploaded_Strategy
// 說明       : 最佳報酬配對
// 核心模型   : 日 K 定錨 + NH/NL 或 Don 確認 + ATR 濾網 + 多層出場引擎 + 1 分 K Open 執行
// 規範       : C1~C5 與交易版完全一致，只有 C6 不同
//=======================================================================

//====================== C1.參數區 ======================
input:
    DonLen(274, "1.Don長度"),
    ATRLen(2, "2.ATR長度"),
    EMAWarmBars(1, "3.EMA定錨回推日數"),
    EntryBufferPts(84, "4.NH/NL突破緩衝點數"),
    DonBufferPts(124, "5.Don突破緩衝點數"),
    MinATRD(127, "6.最小日ATR濾網"),
    ATRStopK(0.57, "7.ATR停損倍數"),
    ATRTakeProfitK(0.81, "8.ATR停利倍率"),
    MaxEntriesPerDay(10, "9.單日最多進場次數"),
    TimeStopBars(115, "10.時間停損Bars"),
    MinRunPctAnchor(0.39, "11.時間停損最小發動(定錨%)"),
    TrailStartPctAnchor(0.71, "12.回吐停利啟動(定錨%)"),
    TrailGivePctAnchor(0.02, "13.回吐停利允許回吐(定錨%)"),
    UseAnchorExit(1, "14.是否啟用08:48定錨失敗出場"),
    AnchorBackPct(0.82, "15.定錨失敗出場(定錨%)"),
    SysHistDBars(600, "98.SysHistDBars"),
    SysHistMBars(20000, "99.SysHistMBars");

//====================== C2.基礎資料與指標計算 ======================
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
    dFieldReady(false),
    dataReady(false),
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
    posFlag(0),
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
    lastExitBar(-9999),
    dayInitDate(0),
    dayRefDate(0),
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

isMinChart = (BarFreq = "Min") and (BarInterval = 1) and (BarAdjusted = false);

if BarFreq <> "Min" then
    RaiseRunTimeError("本腳本僅支援分鐘線");
if BarFreq <> "Min" or BarInterval <> 1 or BarAdjusted then
    RaiseRunTimeError("本腳本僅支援非還原 1 分鐘線");
if DonLen < 1 then
    RaiseRunTimeError("DonLen 必須 >= 1");
if ATRLen < 1 then
    RaiseRunTimeError("ATRLen 必須 >= 1");
if EMAWarmBars < 1 then
    RaiseRunTimeError("EMAWarmBars 必須 >= 1");
if ATRTakeProfitK <= 0 then
    RaiseRunTimeError("ATRTakeProfitK 必須 > 0");

warmupBars = IntPortion(MaxList(fixedMALen3 + 2, fixedEMALen3 + 2, DonLen + 2, ATRLen + 2, EMAWarmBars + 2));

SetBackBar(2);
SetBackBar(SysHistDBars, "D");
SetTotalBar(SysHistMBars);

sessOnEntry  = IFF((Time >= fixedBeginTime) and (Time <= fixedEndTime), 1, 0);
sessOnManage = IFF((Time >= fixedBeginTime) and (Time <= fixedForceExitTime), 1, 0);

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
    lastExitBar = -9999;
    dayInitDate = 0;
    dayRefDate = 0;
    hdrPrinted = false;
    fpath = "C:\XQ\data\" + "[ScriptName]_[Date]_[StartTime].txt";
end;

dayRefDate = 0;
dFieldReady = CheckField("High", "D") and CheckField("Low", "D") and CheckField("Close", "D");
if dFieldReady then
    dayRefDate = GetFieldDate("Close", "D");

if (Date <> dayInitDate) and (Time >= fixedBeginTime) and (dayRefDate = Date) then begin
    yH = GetField("High", "D")[1];
    yL = GetField("Low", "D")[1];
    yC = GetField("Close", "D")[1];

    maSum = 0;
    for i = 1 to fixedMALen2 begin
        maSum = maSum + GetField("Close", "D")[i];
    end;
    ma2D = maSum / fixedMALen2;

    maSum = 0;
    for i = 1 to fixedMALen3 begin
        maSum = maSum + GetField("Close", "D")[i];
    end;
    ma3D = maSum / fixedMALen3;

    alpha2 = 2.0 / (fixedEMALen2 + 1);
    alpha3 = 2.0 / (fixedEMALen3 + 1);

    ema2D = GetField("Close", "D")[EMAWarmBars];
    for i = EMAWarmBars - 1 downto 1 begin
        ema2D = alpha2 * GetField("Close", "D")[i] + (1 - alpha2) * ema2D;
    end;

    ema3D = GetField("Close", "D")[EMAWarmBars];
    for i = EMAWarmBars - 1 downto 1 begin
        ema3D = alpha3 * GetField("Close", "D")[i] + (1 - alpha3) * ema3D;
    end;

    tmpHi = GetField("High", "D")[1];
    tmpLo = GetField("Low", "D")[1];
    for i = 2 to DonLen begin
        if GetField("High", "D")[i] > tmpHi then
            tmpHi = GetField("High", "D")[i];
        if GetField("Low", "D")[i] < tmpLo then
            tmpLo = GetField("Low", "D")[i];
    end;
    donHiD = tmpHi;
    donLoD = tmpLo;

    atrSum = 0;
    for i = 1 to ATRLen begin
        tmpTR = MaxList(
                    GetField("High", "D")[i] - GetField("Low", "D")[i],
                    AbsValue(GetField("High", "D")[i] - GetField("Close", "D")[i + 1]),
                    AbsValue(GetField("Low", "D")[i] - GetField("Close", "D")[i + 1])
                );
        atrSum = atrSum + tmpTR;
    end;
    atrD = atrSum / ATRLen;

    cdpVal = (yH + yL + 2 * yC) / 4;
    nhVal = 2 * cdpVal - yL;
    nlVal = 2 * cdpVal - yH;

    LongBias = false;
    ShortBias = false;

    if ((ma2D > ma3D) or (ema2D > ema3D)) and (yC > cdpVal) then
        LongBias = true;
    if ((ma2D < ma3D) or (ema2D < ema3D)) and (yC < cdpVal) then
        ShortBias = true;

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
    lastExitBar = -9999;
    dayInitDate = Date;
end;

if (Time = fixedBeginTime) and (dayAnchorOpen = 0) then
    dayAnchorOpen = Open;

if dayAnchorOpen > 0 then begin
    minRunPtsByAnchor = dayAnchorOpen * MinRunPctAnchor * 0.01;
    trailStartPtsByAnchor = dayAnchorOpen * TrailStartPctAnchor * 0.01;
    trailGivePtsByAnchor = dayAnchorOpen * TrailGivePctAnchor * 0.01;
    anchorBackPtsByAnchor = dayAnchorOpen * AnchorBackPct * 0.01;
end
else begin
    minRunPtsByAnchor = 0;
    trailStartPtsByAnchor = 0;
    trailGivePtsByAnchor = 0;
    anchorBackPtsByAnchor = 0;
end;

LongEntrySig = false;
ShortEntrySig = false;
LongExitTrig = false;
ShortExitTrig = false;
ForceExitTrig = false;
LongEntryReady = false;
ShortEntryReady = false;
LongExitByATR = false;
ShortExitByATR = false;
LongExitByTP = false;
ShortExitByTP = false;
LongExitByTime = false;
ShortExitByTime = false;
LongExitByTrail = false;
ShortExitByTrail = false;
LongExitByAnchor = false;
ShortExitByAnchor = false;

longEntryLevelNH = nhVal + EntryBufferPts;
longEntryLevelDon = donHiD + DonBufferPts;
shortEntryLevelNL = nlVal - EntryBufferPts;
shortEntryLevelDon = donLoD - DonBufferPts;

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

atrStopLong = cost - ATRStopK * entryATRD;
atrStopShort = cost + ATRStopK * entryATRD;
atrTPPriceLong = cost + ATRTakeProfitK * entryATRD;
atrTPPriceShort = cost - ATRTakeProfitK * entryATRD;

dataReady = isMinChart and (CurrentBar > warmupBars) and dFieldReady and (dayInitDate = Date) and (dayRefDate = Date) and (dayAnchorOpen > 0) and (atrD > 0);

//====================== C3.進場條件 ======================
if dataReady and (sessOnEntry = 1) and (lastMarkBar <> CurrentBar) then begin
    if (posFlag = 0) and (dayEntryCount < MaxEntriesPerDay) then begin
        if LongBias and (atrD >= MinATRD) and ((Open >= longEntryLevelNH) or (Open >= longEntryLevelDon)) then
            LongEntryReady = true;
        if ShortBias and (atrD >= MinATRD) and ((Open <= shortEntryLevelNL) or (Open <= shortEntryLevelDon)) then
            ShortEntryReady = true;

        if LongEntryReady then
            LongEntrySig = true
        else if ShortEntryReady then
            ShortEntrySig = true;
    end;
end;

//====================== C4.出場條件 ======================
if dataReady and (sessOnManage = 1) and (lastMarkBar <> CurrentBar) then begin
    if (Time >= fixedForceExitTime) and (posFlag <> 0) then begin
        ForceExitTrig = true;
    end
    else begin
        if posFlag = 1 then begin
            LongExitByATR = (entryATRD > 0) and (Open <= atrStopLong);
            LongExitByTP = (entryATRD > 0) and (Open >= atrTPPriceLong);
            LongExitByTime = (barsHeld >= TimeStopBars) and (maxRunUpPts < minRunPtsByAnchor);
            LongExitByTrail = (maxRunUpPts >= trailStartPtsByAnchor) and ((bestHighSinceEntry - Open) >= trailGivePtsByAnchor);
            LongExitByAnchor = (UseAnchorExit = 1) and (dayAnchorOpen > 0) and (Open <= dayAnchorOpen - anchorBackPtsByAnchor);
            if LongExitByATR or LongExitByTP or LongExitByTime or LongExitByTrail or LongExitByAnchor then
                LongExitTrig = true;
        end;

        if posFlag = -1 then begin
            ShortExitByATR = (entryATRD > 0) and (Open >= atrStopShort);
            ShortExitByTP = (entryATRD > 0) and (Open <= atrTPPriceShort);
            ShortExitByTime = (barsHeld >= TimeStopBars) and (maxRunUpPts < minRunPtsByAnchor);
            ShortExitByTrail = (maxRunUpPts >= trailStartPtsByAnchor) and ((Open - bestLowSinceEntry) >= trailGivePtsByAnchor);
            ShortExitByAnchor = (UseAnchorExit = 1) and (dayAnchorOpen > 0) and (Open >= dayAnchorOpen + anchorBackPtsByAnchor);
            if ShortExitByATR or ShortExitByTP or ShortExitByTime or ShortExitByTrail or ShortExitByAnchor then
                ShortExitTrig = true;
        end;
    end;
end;

//====================== C5.狀態更新 ======================
hasTradeEvent = false;

if dataReady and (sessOnManage = 1) and (lastMarkBar <> CurrentBar) then begin
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
        lastExitBar = CurrentBar;
        hasTradeEvent = true;
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
        lastExitBar = CurrentBar;
        hasTradeEvent = true;
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
        lastExitBar = CurrentBar;
        hasTradeEvent = true;
    end;
end;

if dataReady and (sessOnEntry = 1) and (lastMarkBar <> CurrentBar) then begin
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
        hasTradeEvent = true;
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
        hasTradeEvent = true;
    end;
end;

longMark = IFF(LongEntrySig, Open, 0);
shortMark = IFF(ShortEntrySig, Open, 0);
longExitMark = IFF(LongExitTrig, Open, 0);
shortExitMark = IFF(ShortExitTrig, Open, 0);
forceExitMark = IFF(ForceExitTrig, Open, 0);

//====================== C6.指標版輸出 ======================
if hasTradeEvent then begin
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
        Print(File(fpath), outStr);
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
        Print(File(fpath), outStr);
    end
    else if ShortEntrySig then begin
        outStr = dateTimeStr + " " + NumToStr(IntPortion(Open), 0) + " 新賣";
        Print(File(fpath), outStr);
    end
    else if LongExitTrig then begin
        outStr = dateTimeStr + " " + NumToStr(IntPortion(Open), 0) + " 平賣";
        Print(File(fpath), outStr);
    end
    else if ShortExitTrig then begin
        outStr = dateTimeStr + " " + NumToStr(IntPortion(Open), 0) + " 平買";
        Print(File(fpath), outStr);
    end
    else if ForceExitTrig then begin
        outStr = dateTimeStr + " " + NumToStr(IntPortion(Open), 0) + " 強制平倉";
        Print(File(fpath), outStr);
    end;
end;

Plot1(longMark, "新買");
Plot2(shortMark, "新賣");
Plot3(longExitMark, "平賣");
Plot4(shortExitMark, "平買");
Plot5(forceExitMark, "強制平倉");
