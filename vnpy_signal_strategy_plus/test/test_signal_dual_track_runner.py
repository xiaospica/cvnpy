from datetime import datetime

import run_signal_dual_track as runner


def test_default_config_resolves_from_vnpy_data_root(monkeypatch, tmp_path):
    monkeypatch.delenv(runner.SIGNAL_DUAL_TRACK_CONFIG_ENV, raising=False)
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))

    assert runner._default_setting_path() == tmp_path / "config" / runner.DEFAULT_SETTING_FILENAME


def test_config_env_override_is_exact(monkeypatch, tmp_path):
    cfg = tmp_path / "custom_signal_dual_track.json"
    monkeypatch.setenv(runner.SIGNAL_DUAL_TRACK_CONFIG_ENV, str(cfg))

    assert runner._default_setting_path() == cfg


def test_load_json_reports_path_and_context(tmp_path):
    cfg = tmp_path / "bad.json"
    cfg.write_text('{"redis": {"stream_key": "harvester_micro_cap_1",}}', encoding="utf-8")

    try:
        runner._load_json(cfg)
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - the test must fail if invalid JSON is accepted.
        raise AssertionError("invalid JSON config was accepted")

    assert str(cfg) in message
    assert "trailing comma" in message
    assert "stream_key" in message


def test_runner_id_is_sanitized_for_shadow_stg():
    assert runner._sanitize_runner_id("Tencent-QMT 01") == "tencent_qmt_01"


def test_runner_id_resolves_from_cli_env_and_config(monkeypatch):
    monkeypatch.setenv(runner.SIGNAL_RUNNER_ID_ENV, "env-runner")

    assert runner._resolve_runner_id("cli-runner", {"runner_id": "config-runner"}) == "cli_runner"
    assert runner._resolve_runner_id("", {"runner_id": "config-runner"}) == "env_runner"

    monkeypatch.delenv(runner.SIGNAL_RUNNER_ID_ENV, raising=False)
    assert runner._resolve_runner_id("", {"dual_track": {"runner_id": "config-runner"}}) == "config_runner"


def test_shadow_stg_defaults_to_runner_scoped_name():
    assert (
        runner._resolve_shadow_stg("v2", "source_stg", "", "local_pc")
        == "source_stg_shadow_local_pc"
    )


def test_shadow_stg_requires_runner_id_for_mirror_modes():
    try:
        runner._resolve_shadow_stg("v3", "source_stg", "", "")
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - the test must fail if shared defaults are accepted.
        raise AssertionError("missing runner_id was accepted")

    assert "--runner-id" in message
    assert runner.SIGNAL_RUNNER_ID_ENV in message


def test_legacy_shared_shadow_stg_is_refused_without_escape_hatch():
    try:
        runner._resolve_shadow_stg("v2", "source_stg", "source_stg_shadow", "local_pc")
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - the test must fail if shared defaults are accepted.
        raise AssertionError("shared shadow stg was accepted")

    assert "Refuse shared shadow stg" in message

    assert (
        runner._resolve_shadow_stg(
            "v2",
            "source_stg",
            "source_stg_shadow",
            "local_pc",
            allow_shared_shadow_stg=True,
        )
        == "source_stg_shadow"
    )


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


def test_sim_setting_prefers_stock_fund_snapshots(monkeypatch, tmp_path):
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))

    setting = runner._sim_setting({"initial_capital": 1_000_000, "sim": {}}, "QMT_SIM_test")

    assert setting["merged_parquet_merged_root"] == str(
        tmp_path / "snapshots" / "merged_stock_fund"
    )
    assert setting["merged_parquet_fallback_roots"] == str(
        tmp_path / "snapshots" / "merged"
    )


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
