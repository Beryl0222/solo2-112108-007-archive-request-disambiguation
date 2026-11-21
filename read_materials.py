"""读取合成业务资料并核对记录字段。"""
import argparse
import json
from pathlib import Path


def read_collection(directory, name):
    """用资料索引校验单份 UTF-8 记录集合。"""
    root = Path(directory)
    manifest = json.loads((root / "index.json").read_text(encoding="utf-8"))
    fields = set(manifest[name])
    with (root / (name + ".jsonl")).open(encoding="utf-8") as source:
        rows = []
        for line_number, line in enumerate(source, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{name} 第 {line_number} 行不是有效的 JSON") from error
            if not isinstance(record, dict) or record.keys() != fields:
                raise ValueError(f"{name} 第 {line_number} 行与资料索引不符")
            rows.append(record)
    return rows


def main():
    parser = argparse.ArgumentParser(description="核对业务资料字段并显示记录数")
    parser.add_argument("collection", nargs="?", help="仅检查指定的资料集合")
    parser.add_argument("--data", default="data", help="含资料索引的目录")
    args = parser.parse_args()
    root = Path(args.data)
    manifest = json.loads((root / "index.json").read_text(encoding="utf-8"))
    names = [args.collection] if args.collection else manifest
    try:
        counts = {name: len(read_collection(root, name)) for name in names}
    except (ValueError, KeyError, OSError) as error:
        parser.exit(2, str(error) + "\n")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
