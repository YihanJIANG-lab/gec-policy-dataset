"""CLI 入口：

    python src/crawl.py --source nea --max-pages 3
    python src/crawl.py --source all --max-pages 2
"""
import argparse
import logging
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import Database  # noqa: E402
from crawler.nea import NeaAdapter  # noqa: E402
from crawler.ndrc import NdrcAdapter  # noqa: E402
from crawler.gzpec import GzpecAdapter  # noqa: E402
from crawler.nmg import NmgAdapter  # noqa: E402

ADAPTERS = {"nea": NeaAdapter, "ndrc": NdrcAdapter, "gzpec": GzpecAdapter,
            "nmg": NmgAdapter}


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
    parser = argparse.ArgumentParser(description="GEC-Radar 绿证政策情报采集")
    parser.add_argument("--source", required=True,
                        help="信源代码（nea / ndrc）或 all 表示全部启用信源")
    parser.add_argument("--max-pages", type=int, default=3,
                        help="每个列表栏目/关键词最多抓取的页数（默认 3）")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "sources.yaml"))
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "gec.db"))
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("gec_radar")

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    keywords = config["keywords"]
    request_cfg = config.get("request", {})

    sources = [s for s in config["sources"] if s.get("enabled")]
    if args.source != "all":
        sources = [s for s in sources if s["code"] == args.source]
        if not sources:
            parser.error(f"未找到启用的信源: {args.source}")

    summaries = []
    with Database(args.db) as db:
        for src in sources:
            code = src["code"]
            if code not in ADAPTERS:
                logger.warning("信源 %s 没有对应适配器，跳过", code)
                continue
            logger.info("=== 开始抓取 %s (%s) ===", src["name"], code)
            adapter = ADAPTERS[code](src, keywords, PROJECT_ROOT, request_cfg)
            summary = adapter.crawl(db, args.max_pages)
            summaries.append(summary)
            logger.info(
                "=== %s 完成：状态=%s 抓取页数=%d 发现条目=%d 新增入库=%d 跳过=%d %s ===",
                src["name"], summary["status"], summary["pages_fetched"],
                summary["items_found"], summary["items_new"],
                summary["items_skipped"],
                f"错误={summary['error']}" if summary["error"] else "",
            )

    print("\n===== 抓取摘要 =====")
    for s in summaries:
        line = (f"{s['source']:<6} 状态={s['status']:<5} 页数={s['pages_fetched']:<3} "
                f"发现={s['items_found']:<4} 新增={s['items_new']:<4} 跳过={s['items_skipped']}")
        if s["error"]:
            line += f"  错误: {s['error']}"
        print(line)
    total_new = sum(s["items_new"] for s in summaries)
    total_found = sum(s["items_found"] for s in summaries)
    print(f"合计：发现 {total_found} 条，新增入库 {total_new} 条")


if __name__ == "__main__":
    main()
