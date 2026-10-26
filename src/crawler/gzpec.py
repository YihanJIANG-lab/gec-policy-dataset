"""广州电力交易中心（gzpec.cn）公告适配器。

- 站点为传统服务端渲染（静态 HTML 栏目页），但 TLS 证书链不完整，
  需要 ssl_verify=False 才能用 requests 访问；
- 列表页 li.MarkList 结构：.date 内含 day + year(2026/09)，.Matitle a 为标题链接；
- 分页为静态页：第 1 页 <entry>，第 N 页 <entry>/index_<N-1>.html；
- “公示公告”栏目跳转外部 JS 应用（ifdl.poweremarket.com），无法直接抓取，
  仅接入“最新通知”栏目。
"""
import logging
import re
from urllib.parse import urljoin

import requests

from crawler.base import BaseAdapter

logger = logging.getLogger("gec_radar")

ARTICLE_RE = re.compile(r"/t\d{8}_\d+\.html")


class GzpecAdapter(BaseAdapter):
    source_code = "gzpec"
    ssl_verify = False  # 站点 TLS 证书链不完整

    def iter_items(self, max_pages):
        for entry_url in self.cfg["entry_urls"]:
            base = entry_url if entry_url.endswith("/") else entry_url + "/"
            for page in range(max_pages):
                page_url = base if page == 0 else urljoin(base, f"index_{page}.html")
                try:
                    html = self.fetch_text(page_url)
                except requests.RequestException:
                    break  # 超出最后一页
                self.polite_sleep()
                soup = self._soup(html)
                items = soup.select("li.MarkList")
                if not items:
                    break  # 无更多分页
                for li in items:
                    a = li.select_one(".Matitle a[href]")
                    if not a or not ARTICLE_RE.search(a["href"]):
                        continue
                    title = a.get_text(strip=True)
                    kws = self.match_keywords(title)
                    if not kws:
                        continue
                    day = li.select_one(".date .day")
                    year = li.select_one(".date .year")
                    pub = None
                    if day and year:
                        y = year.get_text(strip=True).replace("/", "-")
                        pub = f"{y}-{day.get_text(strip=True).zfill(2)}"
                    yield {
                        "url": urljoin(page_url, a["href"]),
                        "title": title,
                        "publish_date": pub,
                        "keywords_matched": kws,
                    }

    @staticmethod
    def _soup(html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "lxml")
