from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MinuteBar:
    date: int
    time: int
    open: float
    high: float
    low: float
    close: float

    @property
    def key(self):
        return (self.date, self.time, self.open, self.high, self.low, self.close)


@dataclass
class DailyBar:
    date: int
    open: float
    high: float
    low: float
    close: float

    @property
    def key(self):
        return (self.date, self.open, self.high, self.low, self.close)


@dataclass
class Trade:
    entry_date: int
    entry_time: int
    entry_price: float
    entry_action: str

    exit_date: int
    exit_time: int
    exit_price: float
    exit_action: str

    direction: int  # 1=long, -1=short

    points: float
    gross_pnl: float
    fee: float
    tax: float
    slip_cost: float
    net_pnl: float


@dataclass
class BacktestResult:
    script_name: str
    params: dict
    trades: list[Trade] = field(default_factory=list)


@dataclass
class ParamSpec:
    name: str
    default: float | int | str
    label: str


@dataclass
class XSParseResult:
    script_name: Optional[str]
    params: list[ParamSpec]
    raw_text: str