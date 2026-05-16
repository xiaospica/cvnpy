"""Replay acceptance capture and comparison CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from vnpy_common.data_paths import strategy_equity_journal_db_path


DEFAULT_STRATEGIES = [
    "etf_rotation_basic",
    "csi300_lgb_headless",
    "csi300_lgb_headless_2",
]
DEFAULT_SQLITE_DBS = [
    Path(r"D:/vnpy_data/state/sim_QMT_SIM.db"),
    Path(r"D:/vnpy_data/state/sim_QMT_SIM_csi300.db"),
    Path(r"D:/vnpy_data/state/sim_QMT_SIM_csi300_2.db"),
    strategy_equity_journal_db_path(),
    Path(r"F:/Quant/code/qlib_strategy_dev/mlearnweb/backend/mlearnweb.db"),
]


def sha256_file(path: Path) -> str:
    """Return the SHA256 hash of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sqlite_tables(db_path: Path) -> list[str]:
    """List SQLite tables."""
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [str(r[0]) for r in rows]


def export_sqlite(
    db_path: Path,
    out_dir: Path,
    strategies: list[str],
    manifest: dict[str, Any],
) -> None:
    """Export SQLite tables to JSONL, filtering strategy tables when possible."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        tables = sqlite_tables(db_path)
    except Exception as exc:
        manifest["warnings"].append(f"sqlite open failed {db_path}: {exc}")
        return

    placeholders = ",".join("?" for _ in strategies)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for table in tables:
            query = f"SELECT * FROM {table}"
            params: list[str] = []
            try:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            except Exception:
                cols = []
            if table in {
                "strategy_equity_journal",
                "strategy_equity_snapshots",
                "ml_metric_snapshots",
                "ml_prediction_daily",
            } and "strategy_name" in cols:
                query += f" WHERE strategy_name IN ({placeholders})"
                params = strategies
            try:
                rows = conn.execute(query, params).fetchall()
            except Exception as exc:
                manifest["warnings"].append(f"sqlite export failed {db_path.name}.{table}: {exc}")
                continue

            path = out_dir / f"{table}.jsonl"
            with path.open("w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")
            manifest["exports"].append(
                {"sqlite": db_path.name, "table": table, "rows": len(rows), "path": str(path)}
            )


def copy_and_export_sqlite(
    source: Path,
    out_root: Path,
    strategies: list[str],
    manifest: dict[str, Any],
) -> None:
    """Backup a SQLite DB, then export tables from the consistent copy."""
    if not source.exists() or source.stat().st_size == 0:
        manifest["warnings"].append(f"db missing or empty: {source}")
        return

    copy_dir = out_root / "sqlite_copies"
    copy_dir.mkdir(parents=True, exist_ok=True)
    copied = copy_dir / source.name
    try:
        with sqlite3.connect(str(source), timeout=10.0) as src_conn:
            with sqlite3.connect(str(copied), timeout=10.0) as dst_conn:
                src_conn.backup(dst_conn)
    except Exception as exc:
        manifest["warnings"].append(
            f"sqlite backup failed {source}: {exc}; fallback to file copy"
        )
        try:
            shutil.copy2(source, copied)
        except Exception as copy_exc:
            manifest["warnings"].append(f"copy failed {source}: {copy_exc}")
            return

    manifest["copied_files"].append(
        {
            "source": str(source),
            "dest": str(copied),
            "sha256": sha256_file(copied),
            "bytes": copied.stat().st_size,
        }
    )
    export_sqlite(copied, out_root / "sqlite_exports" / source.stem, strategies, manifest)


def export_mysql_stock_trade(
    out_root: Path,
    strategies: list[str],
    manifest: dict[str, Any],
) -> None:
    """Export ``stock_trade`` rows for target strategies using local configs."""
    root = Path(__file__).resolve().parents[2]
    config_candidates = [
        root / "vnpy_signal_strategy_plus" / "scripts" / "redis_bridge_setting.local.json",
        root / "vnpy_signal_strategy_plus" / "test" / "redis_live_sim_setting.json",
        root / "vnpy_signal_strategy_plus" / "test" / "test_setting.json",
    ]

    last_error = ""
    rows: list[dict[str, Any]] | None = None
    used_config = ""
    for cfg_path in config_candidates:
        if not cfg_path.exists():
            continue
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg = data.get("mysql")
            if not cfg:
                continue
            import pymysql

            conn = pymysql.connect(
                host=cfg["host"],
                port=int(cfg["port"]),
                user=cfg["user"],
                password=cfg["password"],
                database=cfg.get("db") or cfg.get("database"),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=10,
            )
            try:
                placeholders = ",".join(["%s"] * len(strategies))
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT * FROM stock_trade WHERE stg IN ({placeholders}) "
                        "ORDER BY stg, remark, id",
                        strategies,
                    )
                    rows = list(cur.fetchall())
                used_config = str(cfg_path)
                break
            finally:
                conn.close()
        except Exception as exc:
            last_error = f"{cfg_path.name}: {exc}"

    if rows is None:
        manifest["warnings"].append(f"mysql stock_trade export failed: {last_error or 'config not found'}")
        return

    out_dir = out_root / "mysql_exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "stock_trade.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    manifest["exports"].append(
        {"mysql": "stock_trade", "rows": len(rows), "path": str(path), "config": used_config}
    )


def capture(args: argparse.Namespace) -> Path:
    """Capture replay acceptance artifacts."""
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    label = args.label or "capture"
    out_root = Path(args.output_dir) / f"{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_root.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "label": label,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "strategies": strategies,
        "copied_files": [],
        "exports": [],
        "warnings": [],
    }

    for db_path in DEFAULT_SQLITE_DBS:
        copy_and_export_sqlite(db_path, out_root, strategies, manifest)
    if not args.skip_mysql:
        export_mysql_stock_trade(out_root, strategies, manifest)

    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"capture_dir": str(out_root), "warnings": manifest["warnings"]}, ensure_ascii=False, indent=2))
    return out_root


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _jsonl_exports(root: Path) -> dict[str, Path]:
    """Return JSONL exports keyed by path relative to the capture root."""
    exports: dict[str, Path] = {}
    for path in sorted(root.glob("**/*.jsonl")):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        exports[rel] = path
    return exports


def _normalize_row(filename: str, row: dict[str, Any]) -> dict[str, Any]:
    """Remove volatile DB-only fields before comparison."""
    ignore = {"inserted_at", "created_at", "updated_at"}
    if filename in {"sim_trades.jsonl", "sim_positions.jsonl", "sim_accounts.jsonl"}:
        ignore.add("id")
    elif filename == "sim_orders.jsonl":
        ignore.update({"id", "datetime"})
    elif filename == "strategy_equity_snapshots.jsonl":
        ignore.add("id")
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        if key in ignore:
            continue
        if isinstance(value, float):
            normalized[key] = round(value, 6)
        else:
            normalized[key] = value
    return normalized


def _digest_rows(filename: str, rows: list[dict[str, Any]]) -> str:
    payload = [
        _normalize_row(filename, row)
        for row in rows
    ]
    payload.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def compare(args: argparse.Namespace) -> int:
    """Compare two captured artifact directories."""
    baseline = Path(args.baseline)
    candidate = Path(args.candidate)
    checks: list[dict[str, Any]] = []
    failures = 0

    left_exports = _jsonl_exports(baseline)
    right_exports = _jsonl_exports(candidate)
    target_suffixes = {
        "sim_trades.jsonl",
        "sim_orders.jsonl",
        "sim_positions.jsonl",
        "sim_accounts.jsonl",
        "strategy_equity_journal.jsonl",
        "strategy_equity_snapshots.jsonl",
        "stock_trade.jsonl",
    }
    rel_paths = sorted(
        rel for rel in set(left_exports) | set(right_exports)
        if Path(rel).name in target_suffixes
    )

    for rel in rel_paths:
        filename = Path(rel).name
        left_path = left_exports.get(rel)
        right_path = right_exports.get(rel)
        left_rows = _jsonl_rows(left_path) if left_path else []
        right_rows = _jsonl_rows(right_path) if right_path else []
        left_digest = _digest_rows(filename, left_rows)
        right_digest = _digest_rows(filename, right_rows)
        ok = len(left_rows) == len(right_rows) and left_digest == right_digest
        if not ok:
            failures += 1
        checks.append(
            {
                "artifact": rel,
                "baseline_rows": len(left_rows),
                "candidate_rows": len(right_rows),
                "baseline_digest": left_digest,
                "candidate_digest": right_digest,
                "ok": ok,
            }
        )

    result = {"ok": failures == 0, "failures": failures, "checks": checks}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if failures == 0 else 1


def run(args: argparse.Namespace) -> int:
    """Capture post-refactor artifacts and compare with a baseline directory."""
    post_dir = capture(args)
    args.candidate = str(post_dir)
    return compare(args)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description="QMT_SIM replay acceptance helper")
    sub = parser.add_subparsers(dest="command", required=True)

    capture_p = sub.add_parser("capture", help="capture acceptance artifacts")
    capture_p.add_argument("--label", default="capture")
    capture_p.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    capture_p.add_argument("--output-dir", default=r"F:/Quant/vnpy/vnpy_strategy_dev/artifacts/replay_acceptance")
    capture_p.add_argument("--skip-mysql", action="store_true")
    capture_p.set_defaults(func=capture)

    compare_p = sub.add_parser("compare", help="compare two capture directories")
    compare_p.add_argument("--baseline", required=True)
    compare_p.add_argument("--candidate", required=True)
    compare_p.set_defaults(func=compare)

    run_p = sub.add_parser("run", help="capture post-refactor artifacts and compare")
    run_p.add_argument("--baseline", required=True)
    run_p.add_argument("--scenario", default="three_strategy_live_page")
    run_p.add_argument("--compare", action="store_true")
    run_p.add_argument("--label", default="post_refactor")
    run_p.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    run_p.add_argument("--output-dir", default=r"F:/Quant/vnpy/vnpy_strategy_dev/artifacts/replay_acceptance")
    run_p.add_argument("--skip-mysql", action="store_true")
    run_p.set_defaults(func=run)

    return parser


def main() -> None:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()
    rc = args.func(args)
    if isinstance(rc, int):
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
