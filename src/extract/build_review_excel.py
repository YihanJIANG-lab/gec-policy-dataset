# -*- coding: utf-8 -*-
"""事件对齐、分歧识别与复核工作簿生成。

用法:
    python src/extract/build_review_excel.py                      # 用最新 dual_raw_*.json
    python src/extract/build_review_excel.py --input <raw.json> --out 02_标注与复核/标注包/复核模板.xlsx

配对规则：先按 subtype 精确分组；同组内按 evidence_span 的
difflib.SequenceMatcher 相似度（>=0.5）贪心配对。
配对事件逐字段比较（strength/effective_date/region/price_direction_prior/
object/industry）→ 全部一致为"一致事件"，否则为"字段分歧"；未配对为"召回分歧"。
"""
import argparse
import json
import logging
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("build_review_excel")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)

MODEL_A = "kimi-k3"        # 标注者 A（列名展示为 K3）
MODEL_B = "gpt-6-astra"    # 标注者 B（列名展示为 GPT6）
LABEL_A, LABEL_B = "K3", "GPT6"

# 实质字段：不一致即判"字段分歧"并高亮
COMPARE_FIELDS = ["strength", "effective_date", "region", "price_direction_prior"]
# 措辞字段：自由文本措辞差异不算分歧，一致/分歧表中合并展示为 "K3: xxx / GPT6: yyy"
WORDING_FIELDS = ["object", "industry"]
SIM_THRESHOLD = 0.5

RED_FILL = PatternFill("solid", fgColor="FFF2F2F2")  # 淡红
BOLD = InlineFont(b=True)

VALID_STRENGTHS = ("约束性", "预期性", "倡导性")
# effective_date 归一化时剥离的后缀/修饰（"2020" vs "2020年以来" 视为一致；
# 但精度差异不剥离："2025" vs "2025-01" 仍为分歧）
DATE_SUFFIX_RE = re.compile(r"(以来|年底|年末|底|前|起|后|左右|余年底)$")


def norm_value(ev, field):
    v = ev.get(field)
    if field == "industry":
        if not v:
            return ""
        return ",".join(sorted(str(x).strip() for x in v))
    return ("" if v is None else str(v)).strip()


def norm_substantive(ev, field):
    """实质字段比较前的归一化（仅用于一致性判定，不改写原始值）。"""
    v = norm_value(ev, field)
    if field == "strength":
        # gpt-6 偶发非枚举输出（如"约束性指标/考核"），含哪个枚举子串就归一为哪个
        for s in VALID_STRENGTHS:
            if s in v:
                return s
        return v
    if field == "effective_date":
        # 统一分隔符 -> 去"年"字与"以来/底/前"等后缀，只比较到原文明确的精度；
        # 月/日段补零对齐（"2020-3" 与 "2020-03" 视为一致）
        v = v.replace("/", "-").replace("。", "-").replace(" ", "")
        v = v.replace("年", "-").replace("月", "-").replace("日", "")
        prev = None
        while prev != v:
            prev = v
            v = DATE_SUFFIX_RE.sub("", v)
        segs = [s.zfill(2) if s.isdigit() and i > 0 else s
                for i, s in enumerate(v.split("-")) if s != ""]
        return "-".join(segs)
    if field == "region":
        return v.replace(" ", "")  # "南方区域" vs "广东" 属语义差异，保持分歧
    return v


def merge_wording(ea, eb, field):
    """措辞字段合并展示：相同只显示一份，不同显示 "K3: xxx / GPT6: yyy"。"""
    va, vb = norm_value(ea, field), norm_value(eb, field)
    if va == vb:
        return va
    return f"{LABEL_A}: {va or '∅'} / {LABEL_B}: {vb or '∅'}"


def pair_events(events_a, events_b):
    """返回 (pairs, only_a, only_b)。pairs: [(ev_a, ev_b, sim)]"""
    pairs, only_a = [], []
    used_b = set()
    # 按 subtype 分组
    by_sub_b = {}
    for j, eb in enumerate(events_b):
        by_sub_b.setdefault(eb.get("subtype"), []).append(j)
    for ea in events_a:
        cands = [j for j in by_sub_b.get(ea.get("subtype"), []) if j not in used_b]
        best, best_sim = None, 0.0
        for j in cands:
            sim = SequenceMatcher(None, ea.get("evidence_span", ""),
                                  events_b[j].get("evidence_span", "")).ratio()
            if sim > best_sim:
                best, best_sim = j, sim
        if best is not None and best_sim >= SIM_THRESHOLD:
            pairs.append((ea, events_b[best], best_sim))
            used_b.add(best)
        else:
            only_a.append(ea)
    only_b = [eb for j, eb in enumerate(events_b) if j not in used_b]
    return pairs, only_a, only_b


def classify(events_a, events_b):
    """返回 (agree_pairs, field_conflict_pairs, only_a, only_b)。
    agree_pairs: [(ev_a, ev_b)]；field_conflict_pairs: [(ev_a, ev_b, diff_fields)]
    一致 = 配对成功且实质字段（归一化后）全部一致。"""
    pairs, only_a, only_b = pair_events(events_a, events_b)
    agree, conflict = [], []
    for ea, eb, sim in pairs:
        diffs = [f for f in COMPARE_FIELDS
                 if norm_substantive(ea, f) != norm_substantive(eb, f)]
        if diffs:
            conflict.append((ea, eb, diffs))
        else:
            agree.append((ea, eb))
    return agree, conflict, only_a, only_b


# ---------------- Excel 工具 ----------------

def set_widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def add_dropdown(ws, col_idx, options, first_row, last_row):
    dv = DataValidation(type="list", formula1='"' + ",".join(options) + '"',
                        allow_blank=True, showDropDown=False)
    ws.add_data_validation(dv)
    col = get_column_letter(col_idx)
    dv.add(f"{col}{first_row}:{col}{last_row}")


def header(ws, titles):
    ws.append(titles)
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"


def version_richtext(ev, diff_fields):
    """合并展示事件各字段；差异字段的值加粗。"""
    parts = []
    for f in ["subtype", "actor", "object", "strength", "effective_date",
              "region", "industry", "price_direction_prior"]:
        v = norm_value(ev, f)
        seg = f"{f}={v or '∅'}"
        if f in diff_fields:
            parts.append(TextBlock(BOLD, seg))
        else:
            parts.append(seg)
        parts.append(" | ")
    return CellRichText(*parts[:-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=None, help="dual_raw_*.json，默认取最新")
    ap.add_argument("--seed", default=str(ROOT / "data" / "annotation" / "seed_v1.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "02_标注与复核" / "标注包" / "复核模板.xlsx"))
    args = ap.parse_args()

    raw_path = Path(args.input) if args.input else sorted(
        (ROOT / "data" / "annotation").glob("dual_raw_*.json"))[-1]
    records = json.loads(raw_path.read_text(encoding="utf-8"))
    seed = {d["doc_id"]: d for d in
            (json.loads(l) for l in Path(args.seed).read_text(encoding="utf-8").splitlines() if l.strip())}
    logger.info("读取 %s（%d 条记录），种子文档 %d 篇", raw_path.name, len(records), len(seed))

    # 按文档组织
    by_doc = {}
    for r in records:
        by_doc.setdefault(r["doc_id"], {})[r["model"]] = r

    agree_rows, conflict_rows, only_rows = [], [], []
    field_diff_stat = Counter()
    stats = {"A_total": 0, "B_total": 0, "agree": 0,
             "field_conflict": 0, "only_a": 0, "only_b": 0}
    doc_summary = []

    for doc_id, doc in seed.items():
        recs = by_doc.get(doc_id, {})
        ra, rb = recs.get(MODEL_A), recs.get(MODEL_B)
        ea = (ra or {}).get("events") or []
        eb = (rb or {}).get("events") or []
        stats["A_total"] += len(ea)
        stats["B_total"] += len(eb)
        agree, conflict, only_a, only_b = classify(ea, eb)
        stats["agree"] += len(agree)
        stats["field_conflict"] += len(conflict)
        stats["only_a"] += len(only_a)
        stats["only_b"] += len(only_b)
        for _, _, diffs in conflict:
            field_diff_stat.update(diffs)
        for ea_, eb_ in agree:
            agree_rows.append((doc_id, doc, ea_, eb_))
        for ea_, eb_, diffs in conflict:
            conflict_rows.append((doc_id, doc, ea_, eb_, diffs))
        for ea_ in only_a:
            only_rows.append((doc_id, doc, ea_, None))
        for eb_ in only_b:
            only_rows.append((doc_id, doc, None, eb_))
        n_conflict = len(conflict) + len(only_a) + len(only_b)
        doc_summary.append((doc_id, doc, len(ea), len(eb), n_conflict,
                            (ra or {}).get("status", "缺失"),
                            (rb or {}).get("status", "缺失")))

    # ---- 统计输出 ----
    paired = stats["agree"] + stats["field_conflict"]
    agree_rate = stats["agree"] / paired * 100 if paired else 0.0
    print("\n===== 对齐统计 =====")
    print(f"A({LABEL_A}) 总事件: {stats['A_total']}   B({LABEL_B}) 总事件: {stats['B_total']}")
    print(f"一致事件: {stats['agree']}   字段分歧: {stats['field_conflict']}   "
          f"仅{LABEL_A}有: {stats['only_a']}   仅{LABEL_B}有: {stats['only_b']}")
    print(f"配对事件一致率: {agree_rate:.1f}%（{stats['agree']}/{paired}）")
    print(f"A 对账: {stats['A_total']} = {stats['agree']}+{stats['field_conflict']}+{stats['only_a']} "
          f"= {stats['agree'] + stats['field_conflict'] + stats['only_a']}")
    print(f"B 对账: {stats['B_total']} = {stats['agree']}+{stats['field_conflict']}+{stats['only_b']} "
          f"= {stats['agree'] + stats['field_conflict'] + stats['only_b']}")
    print("字段分歧分布:", dict(field_diff_stat.most_common()))

    # ---- 生成工作簿 ----
    wb = Workbook()

    # Sheet 1: 一致事件
    ws1 = wb.active
    ws1.title = "一致事件"
    header(ws1, ["doc_id", "标题", "subtype", "actor", "object", "strength",
                 "effective_date", "region", "industry", "price_direction_prior",
                 "evidence_span", "人工确认", "备注"])
    for doc_id, doc, ev, ev_b in agree_rows:
        # 措辞字段（object/industry）按合并口径展示；其余字段取 A 版本（实质字段已判一致）
        ws1.append([doc_id, (doc["title"] or "")[:30], ev.get("subtype"),
                    ev.get("actor"),
                    merge_wording(ev, ev_b, "object"), ev.get("strength"),
                    ev.get("effective_date"), ev.get("region"),
                    merge_wording(ev, ev_b, "industry"), ev.get("price_direction_prior"),
                    ev.get("evidence_span"), None, None])
    if agree_rows:
        add_dropdown(ws1, 12, ["确认", "修改", "删除"], 2, len(agree_rows) + 1)
    set_widths(ws1, [8, 30, 8, 18, 20, 10, 14, 10, 16, 14, 50, 10, 16])

    # Sheet 2: 分歧事件
    ws2 = wb.create_sheet("分歧事件")
    header(ws2, ["doc_id", "标题", "subtype", "分歧类型", "差异字段",
                 f"{LABEL_A}版本", f"{LABEL_B}版本",
                 f"evidence_span_{LABEL_A}", f"evidence_span_{LABEL_B}",
                 "人工裁决", "修正后值", "备注"])
    n_conf = 0
    for doc_id, doc, ea_, eb_, diffs in conflict_rows:
        diff_desc = "; ".join(
            f"{f}: {LABEL_A}={norm_value(ea_, f) or '∅'} vs {LABEL_B}={norm_value(eb_, f) or '∅'}"
            for f in diffs)
        ws2.append([doc_id, (doc["title"] or "")[:30], ea_.get("subtype"),
                    "字段分歧", diff_desc,
                    version_richtext(ea_, set(diffs)),
                    version_richtext(eb_, set(diffs)),
                    ea_.get("evidence_span"), eb_.get("evidence_span"),
                    None, None, None])
        n_conf += 1
    for doc_id, doc, ea_, eb_ in only_rows:
        side = f"仅{LABEL_A}有" if ea_ is not None else f"仅{LABEL_B}有"
        ev = ea_ or eb_
        ws2.append([doc_id, (doc["title"] or "")[:30], ev.get("subtype"), side, "",
                    version_richtext(ea_, set()) if ea_ else "",
                    version_richtext(eb_, set()) if eb_ else "",
                    (ea_ or {}).get("evidence_span", ""),
                    (eb_ or {}).get("evidence_span", ""),
                    None, None, None])
        n_conf += 1
    for row in ws2.iter_rows(min_row=2, max_row=n_conf + 1):
        for c in row:
            c.fill = RED_FILL
    if n_conf:
        add_dropdown(ws2, 10, ["采K3", "采GPT6", "修改", "删除", "存疑"], 2, n_conf + 1)
    set_widths(ws2, [8, 30, 8, 12, 40, 60, 60, 40, 40, 12, 20, 16])

    # Sheet 3: 文档列表
    ws3 = wb.create_sheet("文档列表")
    header(ws3, ["doc_id", "source", "publish_date", "标题",
                 f"{LABEL_A}事件数", f"{LABEL_B}事件数", "分歧数",
                 f"{LABEL_A}状态", f"{LABEL_B}状态", "无事件确认"])
    for doc_id, doc, na, nb, nc, sa, sb in doc_summary:
        ws3.append([doc_id, doc.get("source"), doc.get("publish_date"),
                    (doc["title"] or "")[:40], na, nb, nc, sa, sb, None])
    dv = DataValidation(type="list", formula1='"已确认无事件"', allow_blank=True,
                        showDropDown=False)
    ws3.add_data_validation(dv)
    dv.add(f"J2:J{len(doc_summary) + 1}")
    set_widths(ws3, [8, 8, 12, 45, 10, 10, 8, 14, 14, 14])

    # Sheet 4: 说明
    ws4 = wb.create_sheet("说明")
    notes = [
        "复核流程",
        "1. 先处理「分歧事件」Sheet：逐行核对 evidence_span 与原文，在「人工裁决」列选择",
        "   采K3 / 采GPT6 / 修改 / 删除 / 存疑；选「修改」时在「修正后值」列填写修正内容。",
        "   字段分歧行中，两个版本列里加粗的字段值即为不一致处（差异字段列有明细）。",
        "2. 再抽查「一致事件」Sheet ≥20% 的行：在「人工确认」列标 确认/修改/删除。",
        "   一致表中 object / industry 为措辞字段，两模型措辞不同时合并显示为",
        "   “K3: xxx / GPT6: yyy”，措辞差异不算分歧，无需裁决。",
        "3. 两模型均未抽出事件的文档（文档列表中两列事件数均为 0）：",
        "   人工阅读原文确认确实无政策事件后，在「文档列表」的「无事件确认」列打勾。",
        "4. 标注者 A = kimi-k3，标注者 B = gpt-6-astra，temperature=0，同一 SYSTEM_PROMPT。",
        f"5. 数据来源: {raw_path.name}；配对: subtype 精确 + evidence_span 相似度≥{SIM_THRESHOLD} 贪心。",
        "6. 对齐口径（v2）：实质字段 = strength / effective_date / region / price_direction_prior，",
        "   归一化后全部一致才算“一致事件”；措辞字段 = object / industry，不参与分歧判定。",
        "   归一化规则：strength 含枚举子串即归一（如“约束性指标/考核”→约束性）；",
        "   effective_date 统一分隔符并剥离“年/以来/底/前”等后缀（精度差异仍算分歧）；",
        "   region 仅去空格（“南方区域” vs “广东”属语义差异，保持分歧）。",
        "7. 状态列说明: ok=正常; ok_with_validation_errors=事件保留但有个别字段校验告警;",
        "   failed=该 doc×model 调用或解析失败（详见 dual_raw 文件 error 字段）。",
    ]
    for line in notes:
        ws4.append([line])
    ws4.column_dimensions["A"].width = 100

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    logger.info("复核工作簿已生成: %s（一致 %d 行，分歧 %d 行，文档 %d 行）",
                out, len(agree_rows), n_conf, len(doc_summary))

    # 统计落盘，供报告引用
    stats_out = {
        "raw_file": raw_path.name,
        "A_model": MODEL_A, "B_model": MODEL_B,
        "alignment_version": "v2",
        "substantive_fields": COMPARE_FIELDS,
        "wording_fields": WORDING_FIELDS,
        "normalization": {
            "strength": "含枚举子串(约束性/预期性/倡导性)即归一为该枚举",
            "effective_date": "统一分隔符、去“年”字、剥离“以来/底/前/起/后/左右”等后缀；精度差异不剥离",
            "region": "仅去空格，语义差异保持分歧",
        },
        "A_total": stats["A_total"], "B_total": stats["B_total"],
        "agree": stats["agree"], "field_conflict": stats["field_conflict"],
        "only_a": stats["only_a"], "only_b": stats["only_b"],
        "agree_rate_pct": round(agree_rate, 1),
        "field_diff_distribution": dict(field_diff_stat.most_common()),
        "failed": [{"doc_id": r["doc_id"], "model": r["model"], "error": r["error"]}
                   for r in records if r["status"] == "failed"],
    }
    stats_path = ROOT / "data" / "annotation" / "dual_align_stats.json"
    stats_path.write_text(json.dumps(stats_out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("统计已存: %s", stats_path)


if __name__ == "__main__":
    main()
