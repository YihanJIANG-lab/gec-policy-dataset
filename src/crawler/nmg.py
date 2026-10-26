"""内蒙古自治区能源局（nyj.nmg.gov.cn）通知公告适配器。

传统服务端渲染栏目页：
- 列表项为 li > h3 > a（标题），同 li 内 span 为发布日期（YYYY-MM-DD）；
- 分页为静态页：第 1 页 <entry>，第 N 页 <entry>/index_<N-1>.html。
"""
import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from crawler.base import BaseAdapter

logger = logging.getLogger("gec_radar")

ARTICLE_RE = re.compile(r"/t\d{8}_\d+\.html")
DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")


class NmgAdapter(BaseAdapter):
    source_code = "nmg"

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
                soup = BeautifulSoup(html, "lxml")
                links = [a for a in soup.select("li h3 a[href]")
                         if ARTICLE_RE.search(a["href"])]
                if not links:
                    break
                for a in links:
                    title = a.get("title") or a.get_text(strip=True)
                    kws = self.match_keywords(title)
                    if not kws:
                        continue
                    li = a.find_parent("li")
                    pub = None
                    if li:
                        m = DATE_RE.search(li.get_text())
                        if m:
                            pub = m.group(0)
                    yield {
                        "url": urljoin(page_url, a["href"]),
                        "title": title,
                        "publish_date": pub,
                        "keywords_matched": kws,
                    }
