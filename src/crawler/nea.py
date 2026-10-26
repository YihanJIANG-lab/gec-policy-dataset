"""国家能源局（nea.gov.cn）适配器。

列表页由前端 JS 渲染，真实数据在列表页同目录下的 ds_<datasourceId>.json：
  1. GET 列表页 HTML，提取 data="datasource:<32位hex>"；
  2. GET ./ds_<id>.json，取 datasource 数组（每栏目最多 1000 条）；
  3. 与官网一致按每页 25 条切片，max_pages 控制每个栏目处理的页数；
  4. 标题命中关键词的条目才进入详情页抓取（列表级过滤）。
"""
import logging
import re
from urllib.parse import urljoin

from crawler.base import BaseAdapter

logger = logging.getLogger("gec_radar")

PAGE_SIZE = 25  # 与官网分页一致
DATASOURCE_RE = re.compile(r'data="datasource:([0-9a-f]{32})"')


class NeaAdapter(BaseAdapter):
    source_code = "nea"

    def iter_items(self, max_pages):
        for entry_url in self.cfg["entry_urls"]:
            html = self.fetch_text(entry_url)
            self.polite_sleep()
            ds_ids = list(dict.fromkeys(DATASOURCE_RE.findall(html)))
            if not ds_ids:
                logger.warning("[nea] %s 未找到 datasource id", entry_url)
                continue
            for ds_id in ds_ids:
                json_url = urljoin(entry_url, f"./ds_{ds_id}.json")
                data = self.fetch(json_url).json()
                self.pages_fetched += 1
                self.polite_sleep()
                items = data.get("datasource") or []
                cap = min(len(items), PAGE_SIZE * max_pages)
                for it in items[:cap]:
                    url = it.get("publishUrl") or ""
                    if not url.startswith("http"):
                        continue
                    title = self.strip_tags(
                        it.get("showTitle") or it.get("title") or "")
                    kws = self.match_keywords(title)
                    if not kws:
                        continue
                    yield {
                        "url": url,
                        "title": title,
                        "publish_date": (it.get("publishTime") or "")[:10],
                        "keywords_matched": kws,
                    }
