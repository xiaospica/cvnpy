"""Project-root pytest configuration — sys.path setup for test collection.

Adds the vendored ``qlib_strategy_core`` so ``from qlib_strategy_core ...`` and
``from qlib ...`` both resolve without a pip install.

The vendor copy is self-contained — it ships the Microsoft qlib source the vnpy
Python env's mirror doesn't have via pip. No external ``qlib_strategy_dev`` repo
needed (this used to be a fallback; removed in the dependency cleanup).
"""

import sys
from pathlib import Path


def _prepend_sys_path(p: Path) -> None:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


# 0. repo root — 让 `from vnpy_ml_strategy.test.fakes...` / `import run_ml_headless` 等
#    在 pytest 上下文 (例如 test_dual_track_with_fake_live.py 的 importlib.reload) 可解析.
_ROOT = Path(__file__).resolve().parent
_prepend_sys_path(_ROOT)

# 1. qlib_strategy_core submodule (含 Microsoft qlib + 推理 helper)
_CORE_DIR = _ROOT / "vendor" / "qlib_strategy_core"
_prepend_sys_path(_CORE_DIR)
