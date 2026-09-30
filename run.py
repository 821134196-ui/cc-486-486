#!/usr/bin/env python3
"""一条本地启动命令：

    python3 run.py

首次启动或使用 --reset 时：重置 SQLite 与本地图片目录，
并预置超量、断磅、拒签等异常样例，随后启动 Web 服务（默认 127.0.0.1:8000）。
"""
from __future__ import annotations

import argparse

from app import seed
from app.server import serve


def main() -> None:
    ap = argparse.ArgumentParser(description="散装砂浆提货与过磅核对系统（虚构数据）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="重置数据库与图片并重新播种样例")
    ap.add_argument("--seed-only", action="store_true", help="只准备样例数据，不启动服务")
    args = ap.parse_args()

    if args.reset or args.seed_only:
        made = seed.seed("data/app.db", reset=True)
        print(f"样例数据已准备：{len(made)} 笔订单，含 车型不符/装料超量/料仓错选/皮重异常/"
              f"地磅中断/补装超单/含水争议/客户拒签/过号 等异常留痕。")
    if args.seed_only:
        return
    serve(args.host, args.port, reset=False)


if __name__ == "__main__":
    main()
