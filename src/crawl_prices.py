"""价格数据采集 CLI：

    python src/crawl_prices.py --source greenenergy
    python src/crawl_prices.py --source all
"""
import argparse
import logging
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import Database  # noqa: E402
from prices.greenenergy import GreenenergyPriceAdapter  # noqa: E402

ADAPTERS = {"greenenergy": GreenenergyPriceAdapter}


def setup_logging():
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "crawl.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def main():
    parser = argparse.ArgumentParser(description="GEC-Radar 绿证价格数据采集")
    parser.add_argument("--source", required=True,
                        help="价格信源代码（greenenergy）或 all")
    parser.add_argument("--max-pages", type=int, default=40,
                        help="最多抓取页数（默认 40，覆盖全量挂牌）")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "sources.yaml"))
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "gec.db"))
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("gec_radar")

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    request_cfg = config.get("request", {})

    sources = [s for s in config.get("price_sources", []) if s.get("enabled")]
    if args.source != "all":
        sources = [s for s in sources if s["code"] == args.source]
        if not sources:
            parser.error(f"未找到启用的价格信源: {args.source}")

    summaries = []
    with Database(args.db) as db:
        for src in sources:
            code = src["code"]
            if code not in ADAPTERS:
                logger.warning("价格信源 %s 没有对应适配器，跳过", code)
                continue
            logger.info("=== 开始抓取价格 %s (%s) ===", src["name"], code)
            adapter = ADAPTERS[code](src, PROJECT_ROOT, request_cfg)
            summary = adapter.crawl(db, args.max_pages)
            summaries.append(summary)
            logger.info(
                "=== %s 完成：状态=%s 页数=%d 记录=%d 新增=%d 更新=%d %s ===",
                src["name"], summary["status"], summary["pages_fetched"],
                summary["items_found"], summary["items_new"],
                summary["items_updated"],
                f"错误={summary['error']}" if summary["error"] else "",
            )

    print("\n===== 价格抓取摘要 =====")
    for s in summaries:
        line = (f"{s['source']:<12} 状态={s['status']:<5} 页数={s['pages_fetched']:<3} "
                f"记录={s['items_found']:<4} 新增={s['items_new']:<4} 更新={s['items_updated']}")
        if s["error"]:
            line += f"  错误: {s['error']}"
        print(line)
    print(f"合计：记录 {sum(s['items_found'] for s in summaries)} 条，"
          f"新增 {sum(s['items_new'] for s in summaries)} 条")


if __name__ == "__main__":
    main()
