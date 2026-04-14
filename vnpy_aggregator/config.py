"""配置加载. 支持 YAML 或 JSON, 同目录 config.yaml 为默认."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class NodeConfig:
    node_id: str
    base_url: str                   # 例如 https://node1.example.com
    username: str = "vnpy"
    password: str = "vnpy"
    verify_tls: bool = True


@dataclass
class AggregatorConfig:
    host: str = "0.0.0.0"
    port: int = 9000
    jwt_secret: str = "change-me"
    token_expire_minutes: int = 60
    admin_username: str = "admin"
    admin_password: str = "admin"
    heartbeat_interval: float = 10.0
    heartbeat_fail_threshold: int = 3
    nodes: List[NodeConfig] = field(default_factory=list)


def _load_raw(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        import yaml  # 延迟导入, 未安装 yaml 时仍可用 JSON
        return yaml.safe_load(text) or {}
    return json.loads(text)


def load_config(path: Optional[str] = None) -> AggregatorConfig:
    """从 ``AGG_CONFIG`` 环境变量或默认 ``config.yaml`` 加载配置。"""
    candidates: List[Path] = []
    if path:
        candidates.append(Path(path))
    env = os.environ.get("AGG_CONFIG")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).parent / "config.yaml")
    candidates.append(Path(__file__).parent / "config.json")

    raw: dict = {}
    for p in candidates:
        if p.exists():
            raw = _load_raw(p)
            break

    agg_raw: dict = raw.get("aggregator", {}) or {}
    cfg = AggregatorConfig(
        host=agg_raw.get("host", "0.0.0.0"),
        port=int(agg_raw.get("port", 9000)),
        jwt_secret=os.environ.get(
            agg_raw.get("jwt_secret_env", "AGG_JWT_SECRET"),
            agg_raw.get("jwt_secret", "change-me"),
        ),
        token_expire_minutes=int(agg_raw.get("token_expire_minutes", 60)),
        admin_username=agg_raw.get("admin_username", "admin"),
        admin_password=os.environ.get(
            agg_raw.get("admin_password_env", ""), agg_raw.get("admin_password", "admin")
        ),
        heartbeat_interval=float(agg_raw.get("heartbeat_interval", 10.0)),
        heartbeat_fail_threshold=int(agg_raw.get("heartbeat_fail_threshold", 3)),
    )

    for item in raw.get("nodes", []) or []:
        pwd = item.get("password", "vnpy")
        pwd_env = item.get("password_env")
        if pwd_env:
            pwd = os.environ.get(pwd_env, pwd)
        cfg.nodes.append(
            NodeConfig(
                node_id=item["node_id"],
                base_url=item["base_url"].rstrip("/"),
                username=item.get("username", "vnpy"),
                password=pwd,
                verify_tls=bool(item.get("verify_tls", True)),
            )
        )
    return cfg
