"""Project-root pytest configuration — sys.path setup for test collection.

Adds two paths so ``from qlib_strategy_core ...`` and ``from qlib ...`` both
resolve without a pip install:

1. ``vendor/qlib_strategy_core`` — submodule, for new-API inference code
2. ``qlib_strategy_dev`` repo root — supplies the Microsoft qlib source that
   the vnpy Python env does not have via pip (mirror lacks pyqlib)

Both paths update automatically when the submodule or the sibling repo are
refreshed; no reinstall needed.
"""

import os
import sys
from pathlib import Path


def _prepend_sys_path(p: Path) -> None:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


# 0. repo root — 让 `from vnpy_ml_strategy.test.fakes...` / `import run_ml_headless` 等
#    在 pytest 上下文 (例如 test_dual_track_with_fake_live.py 的 importlib.reload) 可解析.
_ROOT = Path(__file__).resolve().parent
_prepend_sys_path(_ROOT)

# 1. qlib_strategy_core submodule
_CORE_DIR = _ROOT / "vendor" / "qlib_strategy_core"
_prepend_sys_path(_CORE_DIR)

# 2. Microsoft qlib source hosted inside qlib_strategy_dev sibling repo.
#    Configurable via env var QLIB_SOURCE_ROOT if the repo lives elsewhere.
_QLIB_SOURCE_DEFAULT = Path(r"F:\Quant\code\qlib_strategy_dev")
_QLIB_SOURCE = Path(os.getenv("QLIB_SOURCE_ROOT", str(_QLIB_SOURCE_DEFAULT)))
if (_QLIB_SOURCE / "qlib" / "__init__.py").exists():
    _prepend_sys_path(_QLIB_SOURCE)
