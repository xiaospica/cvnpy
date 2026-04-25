from __future__ import annotations

from datetime import date

import pytest

from vnpy_qmt_sim.bar_source import (
    BarQuote,
    SimBarSource,
    build_bar_source,
    register_bar_source,
    registered_names,
    unregister_bar_source,
)


def test_builtin_merged_parquet_registered() -> None:
    assert "merged_parquet" in registered_names()


def test_build_unknown_source_raises() -> None:
    with pytest.raises(ValueError, match="unknown bar_source"):
        build_bar_source("no_such_source_xyz")


def test_third_party_source_can_register_and_build() -> None:
    @register_bar_source("unit_test_fake")
    class _FakeSource(SimBarSource):
        name = "unit_test_fake"

        def __init__(self, stub_price: float = 42.0) -> None:
            self.stub_price = float(stub_price)

        def get_quote(self, vt_symbol: str, as_of_date: date) -> BarQuote:
            return BarQuote(
                vt_symbol=vt_symbol,
                as_of_date=as_of_date,
                last_price=self.stub_price,
                pre_close=self.stub_price,
                open_price=self.stub_price,
                high_price=self.stub_price,
                low_price=self.stub_price,
                limit_up=self.stub_price * 1.1,
                limit_down=self.stub_price * 0.9,
                pricetick=0.01,
                name="FAKE",
            )

    try:
        src = build_bar_source("unit_test_fake", stub_price=7.5)
        quote = src.get_quote("000001.SZSE", date(2026, 4, 22))
        assert quote.last_price == 7.5
        assert quote.name == "FAKE"
    finally:
        unregister_bar_source("unit_test_fake")

    assert "unit_test_fake" not in registered_names()


def test_duplicate_registration_raises() -> None:
    @register_bar_source("unit_test_dup_a")
    class _A(SimBarSource):
        def get_quote(self, vt_symbol, as_of_date):
            return None

    try:
        with pytest.raises(ValueError, match="already registered"):
            @register_bar_source("unit_test_dup_a")
            class _B(SimBarSource):
                def get_quote(self, vt_symbol, as_of_date):
                    return None
    finally:
        unregister_bar_source("unit_test_dup_a")
