"""读取本地演练资料。"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "materials"


def list_feeds(root=ROOT):
    return sorted(path.stem for path in root.glob("*.jsonl"))


def read_feed(name, root=ROOT):
    if name not in list_feeds(root):
        raise ValueError(f"未知的资料名称：{name}")
    with (root / f"{name}.jsonl").open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} 第 {number} 行不是有效的 JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{name} 第 {number} 行应为 JSON 对象")
            yield row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed", nargs="?", help="资料名称。省略时列出资料")
    parser.add_argument("--directory", type=Path, default=ROOT, help="本地资料目录")
    args = parser.parse_args()
    if not args.feed:
        print("\n".join(list_feeds(args.directory)))
        return
    try:
        for row in read_feed(args.feed, args.directory):
            print(json.dumps(row, ensure_ascii=False))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

