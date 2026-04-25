# -*- coding: utf-8 -*-
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class BarQuote:
    vt_symbol: str
    as_of_date: date
    last_price: float
    pre_close: float
    open_price: float
    high_price: float
    low_price: float
    limit_up: float
    limit_down: float
    pricetick: float
    name: str = ""
    pct_chg: float = 0.0  # tushare 同名字段，单位 %（已含除权调整，等价于 hfq 涨跌幅）


class SimBarSource(ABC):
    name: str = "base"

    @abstractmethod
    def get_quote(self, vt_symbol: str, as_of_date: date) -> Optional[BarQuote]:
        ...

    def prefetch(self, vt_symbols: list[str], as_of_date: date) -> None:
        return None

    def close(self) -> None:
        return None
