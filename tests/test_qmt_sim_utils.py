from vnpy.trader.constant import Exchange

from vnpy_qmt_sim.utils import parse_symbol_exchange, to_qmt_code


def test_parse_vt_symbol_suffix() -> None:
    symbol, exchange = parse_symbol_exchange("600000.SSE")
    assert symbol == "600000"
    assert exchange == Exchange.SSE


def test_parse_qmt_symbol_suffix() -> None:
    symbol, exchange = parse_symbol_exchange("600000.SH")
    assert symbol == "600000"
    assert exchange == Exchange.SSE


def test_parse_case_insensitive() -> None:
    symbol, exchange = parse_symbol_exchange("000001.sz")
    assert symbol == "000001"
    assert exchange == Exchange.SZSE


def test_to_qmt_code_from_exchange() -> None:
    assert to_qmt_code("600000", Exchange.SSE) == "600000.SH"
    assert to_qmt_code("000001", Exchange.SZSE) == "000001.SZ"

