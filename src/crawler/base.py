"""适配器基类：抓取列表页 -> 解析条目 -> 详情页 -> 入库。

子类只需实现 iter_items(max_pages)，产出 {url, title, publish_date, keywords_matched}。
公共部分：礼貌抓取（UA / 间隔 / 超时 / 指数退避重试）、编码兜底、
原始 HTML 落盘、正文纯文本抽取、关键词匹配、去重与 crawl_log 记录。
"""
import hashlib
import logging
import random
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "GEC-Radar/0.1 (academic research crawler)"
)

logger = logging.getLogger("gec_radar")


class BaseAdapter:
    source_code = "base"
    ssl_verify = True  # 个别政府/交易站点证书链不完整时子类置 False

    def __init__(self, cfg, keywords, project_root, request_cfg=None):
        self.cfg = cfg
        self.keywords = list(keywords)
        self.project_root = Path(project_root)
        rc = request_cfg or {}
        self.timeout = rc.get("timeout", 20)
        self.retries = rc.get("retries", 2)
        self.delay = tuple(rc.get("delay_seconds", [1.0, 2.0]))
        self.pages_fetched = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        if not self.ssl_verify:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self.session.verify = False

    # ---------- 网络 ----------

    def polite_sleep(self):
        time.sleep(random.uniform(*self.delay))

    def fetch(self, url, method="get", **kwargs):
        """带超时、重试（指数退避）与编码兜底的请求。"""
        kwargs.setdefault("timeout", self.timeout)
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                resp.raise_for_status()
                # 政府网站 GBK/UTF-8 混杂：声明不可靠时用 apparent_encoding 兜底
                enc = (resp.encoding or "").lower()
                if not enc or enc in ("iso-8859-1", "ascii"):
                    resp.encoding = resp.apparent_encoding or "utf-8"
                return resp
            except requests.RequestException as e:
                last_err = e
                if attempt < self.retries:
                    wait = (2 ** (attempt + 1)) + random.random()
                    logger.warning("[%s] %s %s 失败(%s)，%.1fs 后第 %d 次重试",
                                   self.source_code, method.upper(), url, e,
                                   wait, attempt + 1)
                    time.sleep(wait)
        raise last_err

    def fetch_text(self, url, **kwargs):
        resp = self.fetch(url, **kwargs)
        self.pages_fetched += 1
        return resp.text

    # ---------- 解析工具 ----------

    @staticmethod
    def strip_tags(html_fragment):
        if not html_fragment:
            return ""
        return BeautifulSoup(html_fragment, "lxml").get_text(strip=True)

    @staticmethod
    def extract_text(html):
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        lines = [l.strip() for l in soup.get_text(separator="\n").splitlines()]
        return "\n".join(l for l in lines if l)

    def match_keywords(self, text):
        if not text:
            return []
        return [k for k in self.keywords if k in text]

    def save_raw(self, url, html):
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()
        rel = Path("data") / "raw" / self.source_code / f"{digest}.html"
        path = self.project_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        return rel.as_posix()

    # ---------- 主流程 ----------

    def iter_items(self, max_pages):
        """产出列表条目：{url, title, publish_date, keywords_matched}"""
        raise NotImplementedError

    def crawl(self, db, max_pages):
        from db import utc_now  # 避免循环依赖

        started_at = utc_now()
        items_found = items_new = items_skipped = 0
        status, error = "ok", None
        try:
            for item in self.iter_items(max_pages):
                items_found += 1
                url = item["url"]
                if db.document_exists(url):
                    db.touch_document(url)  # 去重：只更新 crawl_time
                    items_skipped += 1
                    continue
                try:
                    self.polite_sleep()
                    html = self.fetch_text(url)
                except requests.RequestException as e:
                    logger.warning("[%s] 详情页抓取失败 %s: %s",
                                   self.source_code, url, e)
                    items_skipped += 1
                    continue
                content_text = self.extract_text(html)
                # 列表页或详情页命中任一关键词才入库
                kws = set(item.get("keywords_matched") or [])
                kws |= set(self.match_keywords(item.get("title", "")))
                kws |= set(self.match_keywords(content_text))
                if not kws:
                    items_skipped += 1
                    continue
                db.insert_document({
                    "source": self.source_code,
                    "url": url,
                    "title": item.get("title"),
                    "publish_date": item.get("publish_date"),
                    "content_text": content_text,
                    "raw_path": self.save_raw(url, html),
                    "keywords_matched": ",".join(sorted(kws)),
                })
                items_new += 1
        except Exception as e:  # 单个信源失败不中断整体
            status, error = "error", f"{type(e).__name__}: {e}"
            logger.exception("[%s] 信源抓取中断", self.source_code)
        finally:
            db.log_crawl(self.source_code, started_at, self.pages_fetched,
                         items_found, items_new, status, error)
        return {
            "source": self.source_code,
            "pages_fetched": self.pages_fetched,
            "items_found": items_found,
            "items_new": items_new,
            "items_skipped": items_skipped,
            "status": status,
            "error": error,
        }
