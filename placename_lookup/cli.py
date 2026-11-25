"""命令行入口：run 逐条处理来访记录，ask 处理单条临时来访，review 复核。"""
from __future__ import annotations

import argparse
import json
import sys

from .materials import Materials, Request, parse_time
from .service import LookupService, reader_view


def _load_requests(path):
    requests = []
    with open(path, encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            requests.append(Request(
                row["id"], row["reader"], row["purpose"],
                parse_time(row["at"]), row["previous"], row["text"],
            ))
    return requests


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="placename_lookup",
        description="地名沿革档案的查阅请求辨读与限域摘录",
    )
    parser.add_argument("--data", default="data", help="含资料索引的目录")
    parser.add_argument("--log", default="var/session_log.jsonl", help="会话记录文件")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="按时间顺序处理来访记录")
    run.add_argument("--requests", help="来访记录 JSONL，默认取资料中的 requests")
    run.add_argument("--request-id", help="只显示指定编号的来访结果")

    ask = sub.add_parser("ask", help="处理单条临时来访")
    ask.add_argument("--id", default="adhoc-1", help="本次来访编号")
    ask.add_argument("--reader", required=True)
    ask.add_argument("--purpose", required=True)
    ask.add_argument("--at", required=True, help="带时区的 ISO 8601 时间")
    ask.add_argument("--text", required=True, help="来访人原话")
    ask.add_argument("--previous", help="所补充的上一条来访编号")

    review = sub.add_parser("review", help="有权人员复核当次用版与地名证据")
    review.add_argument("--request-id", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--at", required=True, help="带时区的 ISO 8601 时间")

    args = parser.parse_args(argv)
    service = LookupService(Materials.load(args.data), log_path=args.log)

    if args.command == "run":
        requests = _load_requests(args.requests) if args.requests else service.materials.requests
        results = []
        for request in sorted(requests, key=lambda item: item.at):
            results.append(service.handle_request(request))
        if args.request_id:
            results = [item for item in results if item["request_id"] == args.request_id]
        _print([reader_view(item) for item in results])
    elif args.command == "ask":
        request = Request(
            args.id, args.reader, args.purpose,
            parse_time(args.at), args.previous, args.text,
        )
        _print(reader_view(service.handle_request(request)))
    elif args.command == "review":
        _print(service.review(args.request_id, args.reviewer, parse_time(args.at)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
