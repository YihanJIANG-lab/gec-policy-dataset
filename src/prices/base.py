"""价格数据适配器基类：抓取行情 -> 聚合/解析 -> upsert 入 prices 表。

子类实现 iter_prices(max_pages)，产出 dict：
  {source, platform, cert_type, price_type, price, volume,
   period_granularity, period_start, period_end, url}
复用 crawler.base.BaseAdapter 的礼貌抓取/重试/编码兜底能力。
"""
import logging

import requests

from crawler.base import BaseAdapter

logger = logging.getLogger("gec_radar")


class BasePriceAdapter(BaseAdapter):
    def __init__(self, cfg, project_root, request_cfg=None):
        super().__init__(cfg, [], project_root, request_cfg)

    def iter_prices(self, max_pages):
        raise NotImplementedError

    def crawl(self, db, max_pages):
        from db import utc_now

        started_at = utc_now()
        items_found = items_new = items_updated = 0
        status, error = "ok", None
        try:
            for row in self.iter_prices(max_pages):
                items_found += 1
                row.setdefault("source", self.source_code)
                result = db.upsert_price(row)
                if result == "new":
                    items_new += 1
                else:
                    items_updated += 1
        except Exception as e:  # 单个信源失败不中断整体
            status, error = "error", f"{type(e).__name__}: {e}"
            logger.exception("[%s] 价格信源抓取中断", self.source_code)
        finally:
            db.log_crawl(f"prices:{self.source_code}", started_at,
                         self.pages_fetched, items_found, items_new,
                         status, error)
        return {
            "source": self.source_code,
            "pages_fetched": self.pages_fetched,
            "items_found": items_found,
            "items_new": items_new,
            "items_updated": items_updated,
            "status": status,
            "error": error,
        }
