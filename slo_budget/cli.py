"""同一个入口：接入新一月的记录和申请、本地演练、报表与对账。

许可的真正发出与后果记录都只发生在本地演练输出中，不调用任何外部系统。
资料目录作为参数传入，不依赖当前工作目录，可配合 ``python -I`` 运行。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from .engine import SettlementEngine
from .feed import iter_feed_rows
from .reports import Reporter

FEEDS = ("objectives", "intake", "facts", "changes")


def build_engine(directory: str | Path,
                 feeds: Iterable[str] = FEEDS) -> SettlementEngine:
    """从资料目录读取四类 JSONL 并回放为完整引擎状态。"""
    directory = Path(directory)
    rows: list[dict[str, Any]] = []
    for feed in feeds:
        path = directory / f"{feed}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"资料文件不存在：{path}")
        for row in iter_feed_rows(path, feed):
            rows.append(row)
    return SettlementEngine.replay(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="slo_budget",
        description="错误预算结算与变更额度台（本地演练，无外部副作用）")
    parser.add_argument("--directory", type=Path, default=Path("materials"),
                        help="JSONL 资料目录（默认 materials）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("report", help="输出完整文本报表")
    sub.add_parser("reconcile", help="只输出对账差异与授权依据")
    sub.add_parser("journal", help="输出全量留痕事件 JSONL")

    snapshot = sub.add_parser("snapshot", help="把完整状态快照写入本地文件")
    snapshot.add_argument("--output", type=Path, required=True)

    sub.add_parser("summary", help="输出三套数字的机器可读 JSON 摘要")
    args = parser.parse_args(argv)

    engine = build_engine(args.directory)
    reporter = Reporter(engine)

    if args.command == "report":
        print(reporter.render_text())
    elif args.command == "reconcile":
        items = reporter.reconciliation()
        if not items:
            print("无对账差异")
        for item in items:
            print(json.dumps({
                "period_start": item.period_start,
                "scope": item.scope,
                "request": item.request,
                "authorized_as": item.authorized_as,
                "restated_as": item.restated_as,
                "cause": item.cause,
                "evidence": item.evidence,
            }, ensure_ascii=False))
    elif args.command == "journal":
        for event in reporter.journal():
            print(json.dumps(event, ensure_ascii=False))
    elif args.command == "snapshot":
        engine.save_snapshot(args.output)
        print(f"快照已写入 {args.output}")
    elif args.command == "summary":
        print(json.dumps({
            "live_view": {
                f"{period}|{scope}": view
                for (period, scope), view in engine.ledger.live_view().items()
            },
            "restated_view": {
                f"{period}|{scope}": view
                for (period, scope), view in engine.ledger.restated_view().items()
            },
            "queue": engine.desk.pending_order(),
            "changes": {
                action_id: ticket.as_dict()
                for action_id, ticket in engine.desk.tickets.items()
            },
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
