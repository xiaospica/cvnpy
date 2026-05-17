from datetime import datetime

import run_signal_dual_track as runner


def test_v3_source_is_live_only_and_not_armed_by_default():
    cfg = runner._build_config(
        "v3",
        {"initial_capital": 1_000_000, "sim": {}},
        "source_stg",
        "shadow_stg",
        "paper-account",
    )
    cutoff = datetime(2026, 5, 17, 9, 30)

    runner._apply_live_runtime_options(
        cfg["STRATEGIES"],
        mode="v3",
        allow_live_orders=False,
        live_signal_cutoff_dt=cutoff,
    )

    source = cfg["STRATEGIES"][0]
    shadow = cfg["STRATEGIES"][1]
    qmt_gateway = cfg["GATEWAYS"][0]

    assert qmt_gateway["setting"]["交易账号"] == "paper-account"
    assert qmt_gateway["setting"]["mini路径"]
    assert source["gateway_name"] == "QMT"
    assert source["runtime"]["role"] == "source-live"
    assert source["runtime"]["replay_enabled"] is False
    assert source["runtime"]["live_orders_enabled"] is False
    assert source["runtime"]["live_signal_cutoff_dt"] == cutoff
    assert shadow["gateway_name"] == "QMT_SIM_redis_shadow"
    assert shadow["runtime"]["role"] == "shadow-sim"
    assert shadow["runtime"]["replay_enabled"] is True


def test_v3_cleanup_scope_keeps_source_checkpoint():
    cfg = runner._build_config(
        "v3",
        {"initial_capital": 1_000_000, "sim": {}},
        "source_stg",
        "shadow_stg",
        "paper-account",
    )
    names = runner._strategy_names_for_cleanup(
        cfg["STRATEGIES"],
        ["QMT_SIM_redis_shadow"],
        "shadow",
    )

    assert names == ["shadow_stg"]


def test_v1_alias_matches_single_mode():
    cfg = runner._build_config(
        "v1",
        {"initial_capital": 1_000_000, "sim": {}},
        "source_stg",
        "shadow_stg",
    )

    assert cfg["mirror"] is False
    assert cfg["STRATEGIES"][0]["strategy_name"] == "source_stg"
    assert cfg["STRATEGIES"][0]["runtime"]["role"] == "single-sim"
