"""本地 JSONL 资料读取。

与仓库根的 ``materials.py`` 口径一致，但放在包内，使 ``python -I`` 运行时
不依赖当前工作目录；资料路径始终由调用方显式传入。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


def iter_feed_rows(path: str | Path, feed: str) -> Iterator[dict[str, Any]]:
    path = Path(path)
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{feed} 第 {number} 行不是有效的 JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{feed} 第 {number} 行应为 JSON 对象")
            row["_feed"] = feed
            yield row
