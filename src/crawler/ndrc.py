"""国家发展改革委（ndrc.gov.cn）适配器。

政策栏目众多且部分页面为 JS 渲染，因此走站内智能云搜索：
  1. GET https://so.ndrc.gov.cn/s?qt=<关键词>&siteCode=... 拿到嵌入页面的
     timestamp / wordToken 以及 Set-Cookie 中的 suid（搜索网关用它鉴权）；
  2. POST https://sogw.ndrc.gov.cn/apiservice/s 翻页取 resultList
     （title / url / docDate），搜索在服务端按全文匹配，天然命中关键词；
  3. 按关键词逐个搜索，按 url 合并条目并累计命中的关键词。
"""
import logging
import re
from urllib.parse import urljoin

import requests

from crawler.base import BaseAdapter

logger = logging.getLogger("gec_radar")

PAGE_SIZE = 20  # 与搜索页默认一致
INIT_RE = re.compile(r"initPubProperty\((.*?)\);", re.S)
QUOTED_RE = re.compile(r"'([^']*)'")


class NdrcAdapter(BaseAdapter):
    source_code = "ndrc"

    def _search_tokens(self, keyword):
        """GET 搜索页，解析网关鉴权参数。返回 (api_url, form_base)。"""
        resp = self.fetch(
            self.cfg["search_url"],
            params={"qt": keyword, "siteCode": self.cfg["site_code"],
                    "tab": "all", "toolsStatus": "1"},
        )
        self.pages_fetched += 1
        seg = INIT_RE.search(resp.text)
        if not seg:
            raise RuntimeError("搜索页缺少 initPubProperty，页面结构可能已变化")
        args = QUOTED_RE.findall(seg.group(1))
        # 参数顺序：siteCode, tab, qt, debug, page, pageSize,
        #           timestamp(含千分位逗号), wordToken, tabToken, apiUrl, ...
        api_url = next((a for a in args if a.startswith("http")),
                       self.cfg["api_base"])
        suid = resp.cookies.get("suid") or self.session.cookies.get("suid")
        if not suid:
            raise RuntimeError("未获得 suid cookie，无法调用搜索网关")
        self.session.headers.update({
            "suid": suid,
            "Origin": "https://so.ndrc.gov.cn",
            "Referer": resp.url,
        })
        form = {
            "siteCode": self.cfg["site_code"],
            "tab": "all",
            "timestamp": args[6].replace(",", ""),
            "wordToken": args[7],
            "qt": keyword,
            "timeOption": "0",
            "sort": "dateDesc",
            "keyPlace": "0",
            "fileType": "",
        }
        return urljoin(api_url, "s"), form

    def _search_keyword(self, keyword, max_pages):
        """按单个关键词翻页搜索，产出原始结果 dict 列表。"""
        api, form = self._search_tokens(keyword)
        results = []
        for page in range(1, max_pages + 1):
            data = dict(form, page=str(page), pageSize=str(PAGE_SIZE))
            resp = self.fetch(api, method="post", data=data)
            self.pages_fetched += 1
            self.polite_sleep()
            payload = resp.json()
            search = (payload.get("data") or {}).get("search") or {}
            items = ((search.get("searchResult") or {}).get("resultList")) or []
            for it in items:
                results.append(it)
            if len(items) < PAGE_SIZE:
                break
        return results

    def iter_items(self, max_pages):
        merged = {}  # url -> item，跨关键词去重并累计命中关键词
        for kw in self.keywords:
            try:
                results = self._search_keyword(kw, max_pages)
            except (requests.RequestException, RuntimeError, ValueError) as e:
                logger.warning("[ndrc] 关键词 %r 搜索失败：%s", kw, e)
                continue
            for it in results:
                url = (it.get("url") or "").strip()
                if not url.startswith("http"):
                    continue
                title = self.strip_tags(it.get("title") or it.get("dreTitle"))
                entry = merged.setdefault(url, {
                    "url": url,
                    "title": title,
                    "publish_date": (it.get("docDate") or "")[:10],
                    "keywords_matched": set(),
                })
                entry["keywords_matched"].add(kw)
        for item in merged.values():
            item["keywords_matched"] = sorted(item["keywords_matched"])
            yield item
