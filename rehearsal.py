#!/usr/bin/env python3
"""本地演练运行器：在隔离模式（python -I）下也能显式加载本仓库代码。

用法（资料目录作为参数传入，不依赖当前工作目录）：

    python -I rehearsal.py --directory materials report
    python -I rehearsal.py --directory materials reconcile
    python -I rehearsal.py --directory materials journal
    python -I rehearsal.py --directory materials summary
    python -I rehearsal.py --directory materials snapshot --output state/snapshot.json

真正发出许可、记录后果都只发生在本地输出文件里，无任何外部副作用。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from slo_budget.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
