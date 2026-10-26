"""中国绿色电力证书交易平台（greenenergy.org.cn，原绿证认购平台）价格适配器。

站点为 Vue SPA，但其 /sgcc-apinterface/api/pc/* 公开接口经前端同源代理可直接访问：
  GET /sgcc-apinterface/api/pc/product/list?pageIndex=N&pageSize=1000
返回全部绿证挂牌产品（约 2.7 万条），每条含：
  price（挂牌价，元/张）、sellSales（已售数量）、surplusSales（剩余可售）、
  generateYm（电量所属年月，即证的年份月份）、projectName、projectType。

策略：
  1. 全量抓取挂牌列表，原始 JSON 快照存 data/raw/greenenergy/listings_<date>.json；
  2. 按 (电源类型, 年份月份) 聚合，产出每个分组三行：
     挂牌均价_加权 / 挂牌最低价 / 挂牌最高价，volume = 剩余可售量合计；
  3. period_granularity=day，period_start=period_end=采集日（挂牌快照语义），
     同日重复抓取按去重键更新，跨日形成时间序列。
"""
import json
import logging
from collections import defaultdict
from datetime import date

from prices.base import BasePriceAdapter

logger = logging.getLogger("gec_radar")

PAGE_SIZE = 1000  # 实测可用上限内最大页长，单页约 13s

# 按项目名称关键词分类电源类型（平台字典接口不在代理白名单内，无法直接取映射）
TYPE_RULES = [
    ("光伏", ("光伏", "太阳能", "光热", "农光", "渔光", "分布式发电")),
    ("风电", ("风电", "风能")),
    ("水电", ("水电", "水电站", "水利")),
    ("生物质", ("生物质", "垃圾发电", "沼气", "秸秆")),
    ("储能/其他", ("储能",)),
]


def classify(project_name, project_type):
    name = project_name or ""
    for label, kws in TYPE_RULES:
        if any(k in name for k in kws):
            return label
    return f"未分类(pt={project_type})"


class GreenenergyPriceAdapter(BasePriceAdapter):
    source_code = "greenenergy"
    platform = "中国绿色电力证书交易平台"

    def _fetch_all_listings(self, max_pages):
        api = self.cfg["api_url"]
        listings = []
        page = 1
        while page <= max_pages:
            resp = self.fetch(api, params={"pageIndex": page, "pageSize": PAGE_SIZE})
            self.pages_fetched += 1
            payload = resp.json()
            if payload.get("code") != 200:
                raise RuntimeError(f"接口返回异常: {str(payload)[:200]}")
            rows = (payload.get("data") or {}).get("rows") or []
            total = (payload.get("data") or {}).get("total", 0)
            listings.extend(rows)
            logger.info("[greenenergy] 第 %d 页：%d 条（累计 %d/%d）",
                        page, len(rows), len(listings), total)
            if len(rows) < PAGE_SIZE or len(listings) >= total:
                break
            page += 1
            self.polite_sleep()
        return listings

    def _save_snapshot(self, listings):
        out_dir = self.project_root / "data" / "raw" / self.source_code
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"listings_{date.today().isoformat()}.json"
        path.write_text(json.dumps(listings, ensure_ascii=False), encoding="utf-8")
        logger.info("[greenenergy] 原始挂牌快照已存 %s（%d 条）", path, len(listings))

    def iter_prices(self, max_pages):
        listings = self._fetch_all_listings(max_pages)
        self._save_snapshot(listings)
        today = date.today().isoformat()
        api_url = self.cfg["api_url"]

        groups = defaultdict(list)  # (类型, 年月) -> [(price, surplus)]
        for it in listings:
            price = it.get("price")
            if price is None:
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            surplus = it.get("surplusSales") or 0
            try:
                surplus = float(surplus)
            except (TypeError, ValueError):
                surplus = 0.0
            ym = (it.get("generateYm") or "未知年月").strip() or "未知年月"
            groups[(classify(it.get("projectName"), it.get("projectType")), ym)] \
                .append((price, max(surplus, 0.0)))

        for (type_name, ym), entries in sorted(groups.items()):
            cert_type = f"{type_name}|{ym}"
            prices = [p for p, _ in entries]
            volume = sum(s for _, s in entries)
            weight_sum = sum(s for _, s in entries if s > 0)
            if weight_sum > 0:
                wavg = sum(p * s for p, s in entries) / weight_sum
            else:
                wavg = sum(prices) / len(prices)
            base = {
                "platform": self.platform,
                "cert_type": cert_type,
                "volume": volume,
                "period_granularity": "day",
                "period_start": today,
                "period_end": today,
                "url": api_url,
                # 挂牌快照聚合（非成交价），与 backfill_prices 的 greenenergy_snapshot 同口径
                "quality": "C",
            }
            yield dict(base, price_type="挂牌均价_加权", price=round(wavg, 4))
            yield dict(base, price_type="挂牌最低价", price=min(prices))
            yield dict(base, price_type="挂牌最高价", price=max(prices))
