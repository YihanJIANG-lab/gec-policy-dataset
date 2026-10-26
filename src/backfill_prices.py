# -*- coding: utf-8 -*-
"""回补 2023-08 ~ 2026-08 绿证价格历史数据，写入 prices 表。幂等可复跑。

数据源（quality 分级）：
  A nea_bulletin           国家能源局月度"绿证核发及交易数据"通报（2024-07 ~ 2026-07，分电量生产年）
  A nea_index              中国绿证价格指数官方文本值（2026-06/07/08，基期 2025-01=100）
  A nea_report_2024        《中国绿色电力证书发展报告(2024)》：2024 全年单独交易均价 5.59 元/个
  A gzpec_weekly           广州电力交易中心"绿证交易每周行情一览"（2025-09 起，周度）
  B news_citation          新闻报道引用的官方数据（保留出处链接）
  C nea_index_derived      由官方指数环比/同比反推的相邻点
  C greenenergy_snapshot   从挂牌快照反推各年份证当前挂牌价

价格单位：元/个（=元/张，1 证 = 1000 kWh）；数量单位统一换算为 个（原文"万个"×10000）。
未区分电量生产年的来源 cert_type 留空。
"""
import calendar
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import Database  # noqa: E402

logger = logging.getLogger("backfill_prices")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
NEA_ZZZX_JSON = "https://www.nea.gov.cn/sjzz/zzglzx/ds_f59d27ca10a04439bdc57a8055d721a2.json"
NEA_ZZZX_BASE = "https://www.nea.gov.cn/sjzz/zzglzx/index.htm"


def get(url, verify=True, retries=2):
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=UA, timeout=20, verify=verify)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            time.sleep(1.2)
            return r.text
        except requests.RequestException as e:
            if attempt >= retries:
                raise
            time.sleep(3 * (attempt + 1))


def page_text(html):
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    return "\n".join(l.strip() for l in soup.get_text(separator="\n").splitlines() if l.strip())


def flat_text(html):
    """NEA/gzpec 部分页面把每个数字拆进独立 span（"202\\n6\\n年"），
    需压平成全连续文本再做正则解析。"""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    return "".join(l.strip() for l in soup.get_text(separator="\n").splitlines() if l.strip())


def month_period(y, m):
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def nea_list():
    """从资质中心栏目 JSON 枚举全部绿证月度通报与价格指数链接。"""
    data = json.loads(get(NEA_ZZZX_JSON))
    bulletins, indexes = [], []
    for it in data.get("datasource", []):
        title = BeautifulSoup(it.get("showTitle") or "", "lxml").get_text(strip=True)
        url = urljoin(NEA_ZZZX_BASE, it.get("publishUrl") or "")
        m = re.search(r"发布(\d{4})年(\d{1,2})月全国可再生能源绿色电力证书核发及交易数据", title)
        if m:
            bulletins.append((int(m.group(1)), int(m.group(2)), url))
            continue
        m = re.fullmatch(r"(\d{4})年(\d{1,2})月中国绿证价格指数", title)
        if m:
            indexes.append((int(m.group(1)), int(m.group(2)), url))
    return sorted(bulletins), sorted(indexes)


def backfill_nea_bulletins(rows):
    """A级：NEA 月度通报。解析单独交易总量/均价与分电量生产年量价。

    格式演变：2024-07~2025-03 只有核发/交易量无价格；2025-04 起价格在一览表
    （HTML table）；部分月份正文另有分年份价格句；个别月份价格表为图片（抓不到）。
    """
    bulletins, _ = nea_list()
    raw_dir = ROOT / "data" / "raw" / "nea_bulletin"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for y, m, url in bulletins:
        html = get(url)
        (raw_dir / f"{y}-{m:02d}.html").write_text(html, encoding="utf-8")
        text = flat_text(html)
        ps, pe = month_period(y, m)
        base = {"source": "nea_bulletin", "platform": "全国（国家能源局通报）",
                "period_granularity": "month", "period_start": ps, "period_end": pe,
                "url": url, "quality": "A"}
        found = []
        # 全部交易（含绿电交易）总量
        mt = re.search(r"全国交易绿证([\d.]+)(亿|万)个", text)
        if mt:
            vol = float(mt.group(1)) * (1e8 if mt.group(2) == "亿" else 1e4)
            rows.append(dict(base, cert_type="", price_type="月交易总量（含绿电交易）",
                             price=None, volume=vol))
            found.append("总量")
        # 单独交易总量 + 整体均价（措辞有"全国单独交易绿证"/"全国绿证单独交易"两种）
        mv = re.search(r"全国(?:绿证)?单独交易(?:绿证)?([\d.]+)万个", text)
        mp = re.search(r"全国(?:绿证)?单独交易(?:绿证)?[\d.]+万个，交易平均价格为?([\d.]+)元/个",
                       text)
        if mv or mp:
            rows.append(dict(base, cert_type="", price_type="单独交易月均价",
                             price=float(mp.group(1)) if mp else None,
                             volume=float(mv.group(1)) * 1e4 if mv else None))
            found.append("单独整体")
        # 分电量生产年：正文句子（"交易650万个，平均价格1.25元/个" 或 "交易平均价格1.12元/个"）
        for vy, vol, price in re.findall(
                r"电量生产年为(\d{4})年的绿证(?:单独)?交易(?:(\d+)万个，)?平均价格(?:为)?([\d.]+)元/个",
                text):
            rows.append(dict(base, cert_type=f"{vy}年证", price_type="单独交易月均价",
                             price=float(price),
                             volume=float(vol) * 1e4 if vol else None))
            found.append(f"{vy}年证(句)")
        # 分电量生产年：HTML 价格表（2025-04 起；含"2023年及以前"档）
        soup = BeautifulSoup(html, "lxml")
        for tb in soup.find_all("table"):
            trs = [[c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                   for tr in tb.find_all("tr")]
            if not trs or not any("生产年" in c for c in trs[0]):
                continue
            for cells in trs[1:]:
                if len(cells) < 3:
                    continue
                label = cells[0].replace("年", "")
                if "总计" in cells[0]:
                    continue
                if not re.fullmatch(r"\d{4}(及以前)?", label):
                    continue
                ct = f"{label}年证"
                if any(r["source"] == "nea_bulletin" and r["cert_type"] == ct
                       and r["period_start"] == ps for r in rows):
                    continue  # 句子已解析过同年份
                try:
                    vol = float(cells[1].replace(",", "")) if cells[1] else None
                    price = float(cells[2]) if cells[2] else None
                except ValueError:
                    continue
                rows.append(dict(base, cert_type=ct, price_type="单独交易月均价",
                                 price=price,
                                 volume=vol * 1e4 if vol else None))
                found.append(f"{ct}(表)")
        logger.info("nea_bulletin %d-%02d: %s", y, m, found or "无量价（价格表为图片）")


def backfill_nea_index(rows):
    """A级：价格指数官方文本值 + C级：环比/同比反推相邻点。"""
    _, indexes = nea_list()
    # 指数发布首文（2026-06 值）标题不在 "YYYY年M月中国绿证价格指数" 模式内，显式加入
    indexes = indexes + [(2026, 6,
        "https://www.nea.gov.cn/20260724/808ea736bb9741909dd46f8cb32ea0de/c.html")]
    for y, m, url in indexes:
        text = page_text(get(url))
        v = re.search(r"收于([\d.]+)点", text)
        mom = re.search(r"环比(上涨|下跌)([\d.]+)%", text)
        yoy = re.search(r"同比(上涨|下跌)([\d.]+)%", text)
        if not v:
            continue
        val = float(v.group(1))
        ps, pe = month_period(y, m)
        rows.append({"source": "nea_index", "platform": "全国（中国绿证价格指数）",
                     "cert_type": "综合", "price_type": "中国绿证价格指数",
                     "price": val, "volume": None,
                     "period_granularity": "month", "period_start": ps,
                     "period_end": pe, "url": url, "quality": "A"})
        # C级反推：上月 = 本月/(1±环比)；去年同月 = 本月/(1±同比)
        if mom:
            sign = 1 if mom.group(1) == "上涨" else -1
            prev_val = round(val / (1 + sign * float(mom.group(2)) / 100), 1)
            py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
            ps2, pe2 = month_period(py, pm)
            rows.append({"source": "nea_index_derived", "platform": "全国（中国绿证价格指数）",
                         "cert_type": "综合", "price_type": "中国绿证价格指数_环比反推",
                         "price": prev_val, "volume": None,
                         "period_granularity": "month", "period_start": ps2,
                         "period_end": pe2, "url": url, "quality": "C"})
        if yoy:
            sign = 1 if yoy.group(1) == "上涨" else -1
            yago = round(val / (1 + sign * float(yoy.group(2)) / 100), 1)
            ps3, pe3 = month_period(y - 1, m)
            rows.append({"source": "nea_index_derived", "platform": "全国（中国绿证价格指数）",
                         "cert_type": "综合", "price_type": "中国绿证价格指数_同比反推",
                         "price": yago, "volume": None,
                         "period_granularity": "month", "period_start": ps3,
                         "period_end": pe3, "url": url, "quality": "C"})
        logger.info("nea_index %d-%02d: %s 点", y, m, val)


def backfill_gzpec_weekly(rows):
    """A级：广州电力交易中心绿证周报（周度交易均价+交易量+分电量生产年表）。"""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    raw_dir = ROOT / "data" / "raw" / "gzpec_weekly"
    raw_dir.mkdir(parents=True, exist_ok=True)
    links = []
    for p in range(26):
        url = ("https://www.gzpec.cn/news/scyj/" if p == 0
               else f"https://www.gzpec.cn/news/scyj/index_{p}.html")
        soup = BeautifulSoup(get(url, verify=False), "lxml")
        for li in soup.select("li.MarkList"):
            a = li.select_one(".Matitle a[href]")
            if a and "绿证交易每周行情" in a.get_text(strip=True):
                links.append(urljoin(url, a["href"]))
        if not soup.select("li.MarkList"):
            break
    logger.info("gzpec 周报链接 %d 个", len(links))
    ok = 0
    for url in links:
        html = get(url, verify=False)
        text = flat_text(html)
        mw = re.search(r"（(\d{4})年第(\d+)[周期]）", text)
        # 日期有两种格式："2026年2月24日-3月1日" 与（早期）"9月22日-9月26日"（缺年份）
        mp = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日-(?:(\d{1,2})月)?(\d{1,2})日", text)
        mv = re.search(r"绿证交易([\d.]+)万个", text)
        mpr = re.search(r"(?:交易)?均价([\d.]+)元/个", text)
        if not (mw and mp and (mv or mpr)):
            logger.warning("gzpec 周报解析失败: %s", url)
            continue
        y, w = int(mw.group(1)), int(mw.group(2))
        yy = mp.group(1) or str(y)  # 早期格式缺年份，用标题年份
        m1 = int(mp.group(2))
        m2 = int(mp.group(4)) if mp.group(4) else m1
        ps = f"{yy}-{m1:02d}-{int(mp.group(3)):02d}"
        pe = f"{yy}-{m2:02d}-{int(mp.group(5)):02d}"
        (raw_dir / f"{y}w{w:02d}.html").write_text(html, encoding="utf-8")
        rows.append({"source": "gzpec_weekly", "platform": "广州电力交易中心",
                     "cert_type": "", "price_type": "周交易均价",
                     "price": float(mpr.group(1)) if mpr else None,
                     "volume": float(mv.group(1)) * 1e4 if mv else None,
                     "period_granularity": "week", "period_start": ps,
                     "period_end": pe, "url": url, "quality": "A"})
        # 分电量生产年周度表（如有 HTML table）
        for tb in BeautifulSoup(html, "lxml").find_all("table"):
            trs = [[c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                   for tr in tb.find_all("tr")]
            if not trs or not any("生产年" in c for c in trs[0]):
                continue
            for cells in trs[1:]:
                label = cells[0].replace("年", "") if cells else ""
                if not re.fullmatch(r"\d{4}(及以前)?", label):
                    continue
                try:
                    vol = float(cells[1].replace(",", "")) if len(cells) > 1 else None
                    price = float(cells[2]) if len(cells) > 2 else None
                except (ValueError, IndexError):
                    continue
                rows.append({"source": "gzpec_weekly", "platform": "广州电力交易中心",
                             "cert_type": f"{label}年证", "price_type": "周交易均价",
                             "price": price,
                             "volume": vol * 1e4 if vol else None,
                             "period_granularity": "week", "period_start": ps,
                             "period_end": pe, "url": url, "quality": "A"})
        ok += 1
    logger.info("gzpec 周报解析成功 %d/%d", ok, len(links))


def backfill_nea_report_2024(rows):
    """A级：《中国绿色电力证书发展报告(2024)》年度均价（官方 PDF，已存档）。"""
    rows.append({"source": "nea_report_2024", "platform": "全国",
                 "cert_type": "", "price_type": "全年单独交易均价",
                 "price": 5.59, "volume": None,
                 "period_granularity": "year",
                 "period_start": "2024-01-01", "period_end": "2024-12-31",
                 "url": "https://www.nea.gov.cn/download/zglsdlzsfzbg2024.pdf",
                 "quality": "A"})


# B级：新闻报道引用的官方数据（逐条人工核对原文后录入，保留出处）
NEWS_CITATIONS = [
    # (period_start, period_end, granularity, platform, price_type, price, url, caliber_note)
    ("2024-01-01", "2024-01-31", "month", "广州电力交易中心（粤港澳）", "月度交易均价", 12.8,
     "http://yuanchuang.caijing.com.cn/2025/0513/5089921.shtml",
     "财经网2025-05-13报道引广州电力交易中心数据：2024年1月均价最高12.8元/个"),
    ("2024-12-01", "2024-12-31", "month", "广州电力交易中心（粤港澳）", "月度交易均价", 1.1,
     "http://yuanchuang.caijing.com.cn/2025/0513/5089921.shtml",
     "同上：2024年12月均价最低1.1元/个"),
    ("2024-01-01", "2024-12-31", "year", "广州电力交易中心（粤港澳）", "年度交易均价", 2.7,
     "http://yuanchuang.caijing.com.cn/2025/0513/5089921.shtml",
     "同上：2024年粤港澳地区绿证交易均价2.7元/个"),
    ("2025-04-01", "2025-04-30", "month", "广州电力交易中心", "月度交易均价", 1.8,
     "http://yuanchuang.caijing.com.cn/2025/0513/5089921.shtml",
     "同上：2025年4月价格区间0.9-3.4元/个，均价1.8元/个"),
    ("2025-04-01", "2025-12-31", "month_range", "全国", "区间加权均价", 4.39,
     "https://rmi.org.cn/wp-content/uploads/2026/05/final-0508-2026电力市场化改革与电价体系洞察.pdf",
     "RMI《2026电力市场化改革与电价体系洞察》引官方数据：2025年4-12月全国绿证交易加权均价4.39元/个"),
]


def backfill_news_citations(rows):
    for ps, pe, gran, platform, ptype, price, url, note in NEWS_CITATIONS:
        rows.append({"source": "news_citation", "platform": platform,
                     "cert_type": "", "price_type": ptype, "price": price,
                     "volume": None, "period_granularity": gran,
                     "period_start": ps, "period_end": pe,
                     "url": f"{url} （{note}）", "quality": "B"})


def backfill_snapshot(rows):
    """C级：从绿证认购平台挂牌快照反推各年份证当前挂牌价（非成交价）。"""
    snap_dir = ROOT / "data" / "raw" / "greenenergy"
    snaps = sorted(snap_dir.glob("listings_*.json"))
    if not snaps:
        logger.warning("无挂牌快照，跳过")
        return
    snap_path = snaps[-1]
    snap_date = snap_path.stem.replace("listings_", "")
    listings = json.loads(snap_path.read_text(encoding="utf-8"))
    groups = {}
    for it in listings:
        ym, p = it.get("generateYm"), it.get("price")
        if ym and p is not None:
            groups.setdefault(ym[:4], []).append(
                (float(p), float(it.get("surplusSales") or 0)))
    y, m = int(snap_date[:4]), int(snap_date[5:7])
    ps, pe = month_period(y, m)
    for vy, entries in sorted(groups.items()):
        w = sum(s for _, s in entries if s > 0)
        avg = (sum(p * s for p, s in entries) / w if w
               else sum(p for p, _ in entries) / len(entries))
        rows.append({"source": "greenenergy_snapshot",
                     "platform": "中国绿色电力证书交易平台",
                     "cert_type": f"{vy}年证", "price_type": "挂牌现价_回补推断",
                     "price": round(avg, 4), "volume": sum(s for _, s in entries),
                     "period_granularity": "month", "period_start": ps,
                     "period_end": pe, "url": f"data/raw/greenenergy/{snap_path.name}",
                     "quality": "C"})
        logger.info("snapshot %s年证 挂牌加权均价 %.2f（%d 条挂牌）", vy, avg, len(entries))


def coverage_report(db):
    """2023-08 ~ 2026-08 共 38 个月的覆盖矩阵。"""
    conn = sqlite3.connect(str(ROOT / "data" / "gec.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT period_granularity g, period_start ps, period_end pe, quality q, "
        "source s, price_type pt, price, cert_type ct FROM prices").fetchall()
    conn.close()
    months = []
    y, m = 2023, 8
    while (y, m) <= (2026, 8):
        months.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    matrix = {}
    for mo in months:
        price_hits, vol_hits = [], []
        for r in rows:
            if not r["ps"]:
                continue
            ps, pe = r["ps"][:7], (r["pe"] or r["ps"])[:7]
            if r["g"] in ("week", "month", "month_range") and not (pe < mo or ps > mo):
                (price_hits if r["price"] is not None else vol_hits).append(r)
            # year 粒度不计入月度覆盖，单独在报告末尾列出
        if price_hits:
            matrix[mo] = {"grade": min(h["q"] for h in price_hits),
                          "sources": sorted({h["s"] for h in price_hits}),
                          "prices": [h["price"] for h in price_hits]}
        elif vol_hits:
            matrix[mo] = {"grade": "量", "sources": sorted({h["s"] for h in vol_hits}),
                          "prices": []}
        else:
            matrix[mo] = {"grade": None, "sources": [], "prices": []}
    out = ROOT / "data" / "price_coverage.json"
    out.write_text(json.dumps(matrix, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n===== 月度覆盖矩阵（2023-08 ~ 2026-08；[量]=只有成交量无价格）=====")
    for mo, v in matrix.items():
        bar = v["grade"] or "·"
        print(f"  {mo}  [{bar}]  {','.join(v['sources'])}")
    cov = sum(1 for v in matrix.values() if v["grade"] and v["grade"] != "量")
    vol_only = sum(1 for v in matrix.values() if v["grade"] == "量")
    print(f"价格覆盖 {cov}/38 个月，仅成交量 {vol_only} 个月，空白 {38 - cov - vol_only} 个月")
    print("年度数据点: ", [(r["ps"], r["pt"], r["price"]) for r in rows if r["g"] == "year"])
    print(f"矩阵已存: {out}")


def main():
    db_path = ROOT / "data" / "gec.db"
    rows = []
    for fn in (backfill_nea_bulletins, backfill_nea_index, backfill_nea_report_2024,
               backfill_gzpec_weekly, backfill_news_citations, backfill_snapshot):
        try:
            fn(rows)
        except Exception as e:
            logger.exception("数据源 %s 失败（不中断整体）: %s", fn.__name__, e)
    logger.info("待入库 %d 行", len(rows))
    new = upd = 0
    with Database(db_path) as db:
        for r in rows:
            if db.upsert_price(r) == "new":
                new += 1
            else:
                upd += 1
    print(f"\n入库完成：新增 {new}，更新 {upd}")
    coverage_report(db_path)


if __name__ == "__main__":
    main()
