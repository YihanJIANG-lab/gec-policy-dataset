# -*- coding: utf-8 -*-
"""v4.2 offline CPU diagnostics: 96 placebo seed jobs, 8 single-feature jobs, 4 new lag jobs plus 4 primary reuses, 32 paired bootstrap intervals. Every diagnostic prediction fold is saved at original precision. Tables are generated from the same completed result artifact."""
import datetime as dt
import json
import logging
import sys
import time
import hashlib
import copy
import importlib.metadata
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_experiments as R  # noqa: E402
from models import model_registry  # noqa: E402

logger = logging.getLogger("forecast_diag")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)

DATASET = ROOT / "data" / "forecast_dataset_v4_2.json"
RESULTS = ROOT / "data" / "forecast_results_v4_2.json"
PAPER = ROOT / "03_论文投稿" / "paper"
ZERO = [0, 0, 0, 0]
PLACEBO_SEEDS = [11, 22, 33]
EVENT_MODELS = ["ARIMA", "LSTM", "PatchTST", "Prophet"]


def get_xy(s):
    if s["freq"] == "W":
        periods, y, _ = R.to_weekly(s["points"])
    else:
        periods, y = [p for p, _ in s["points"]], [v for _, v in s["points"]]
    return periods, y


def eval_variant(y, periods, exog_map, m, init, win, exog_override=None, observed_periods=None):
    """单模型滚动评估，返回 (metrics, folds_detail)。exog_override 可替换外生序列（placebo/单特征）。"""
    xmap = exog_override if exog_override is not None else exog_map
    lag = m.get("lag", 0)
    folds = []
    for t in range(init, len(y)):
        if observed_periods is not None and periods[t] not in observed_periods:
            continue
        train_y = y[:t]
        src = lambda i: xmap.get(periods[i-lag], ZERO) if i-lag >= 0 else ZERO
        tx = src(t)
        train_x = [src(i) for i in range(t)] if m["exog"] else None
        if m.get("prophet"):
            from models import prophet_base, prophet_exog
            fn = prophet_exog if m["exog"] else prophet_base
            pred = fn(train_y, train_x, tx, periods[:t], periods[t])
        elif m["exog"]:
            pred = m["fn"](train_y, train_x, tx)
        else:
            pred = m["fn"](train_y, None, None)
        folds.append((y[t], pred, periods[t], periods[t] in win, y[t - 1]))
    met = R.metrics(folds)
    det = [[f[2], float(f[0]), float(f[1]), float(f[4]), bool(f[3])]
           for f in folds]
    return met, det


def placebo_map(exog_map, periods, seed):
    """把含事件的期的特征向量随机重排到随机期（保持事件数量与强度分布）。"""
    rng = np.random.default_rng(seed)
    vecs = [exog_map.get(p, ZERO) for p in periods]
    nz = [i for i, v in enumerate(vecs) if v[0] > 0]
    dst = rng.permutation(len(periods))[:len(nz)]
    out = {p: list(ZERO) for p in periods}
    for si, di in zip(nz, dst):
        out[periods[di]] = vecs[si]
    return out


def single_feature_map(exog_map, periods, col):
    return {p: [exog_map.get(p, ZERO)[col]] for p in periods}


def write_main_forecast_tex(d):
    """主 Table 2：仅周度核心模型，oracle/lag1/placebo 关键列。月度/消融/CI 在附录。"""
    res = d["results"]["weekly_overall"]["models"]
    pla = {(p["family"], p["setting"]): p["placebo_MAE_mean"] for p in d.get("placebo", [])
           if p["series"] == "weekly_overall"}
    fams = ["ARIMA", "LSTM", "PatchTST", "Prophet"]
    lines = [
        "% Auto-generated: src/forecast/run_diagnostics.py -- main Table 2 (weekly core, v4.2)",
        "% Weekly:52 grid points,47 observed;5 causal-forward-filled training weeks;23 observed evaluation targets.",
        "% +Oracle = same-period features by retrospective assumption; +Lag1 = one-period event proxy;",
        "% +Placebo = date-permuted event features, mean of 3 seeds. DirAcc tie-aware.",
        "% SNaive(4): period-4 seasonal naive as robustness reference (series too short to",
        "% identify genuine seasonality; included for completeness).",
        "\\begin{table*}[t]",
        "  \\centering",
        "  \\footnotesize",
        "  \\caption{Weekly GEC price forecasting (rolling-origin, $h$=1; MAE in CNY/certificate). "
        "Monthly series, feature-group ablation, lag scan and per-cell CIs are in Appendix~\\ref{app:forecast}.}",
        "  \\label{tab:forecast}",
        "  \\begin{tabular}{lcccccc}",
        "    \\toprule",
        "    Model & Base & +Oracle & +Lag1 & +Placebo & Dir.Acc(\\%) & Move(\\%) \\\\",
        "    \\midrule",
    ]
    for m in ("Naive", "SNaive(4)", "Chronos-t5-tiny(zs)"):
        r = res.get(m)
        if not r or r.get("status") != "ok":
            continue
        da = "--" if r["DirAcc"] is None else f"{r['DirAcc']:.0f}"
        lines.append(f"    {m} & {r['MAE']:.3f} & -- & -- & -- & {da} & "
                     f"{r['MoveRate']:.0f} \\\\")
    lines.append("    \\midrule")
    for fam in fams:
        b = res[fam]
        o = res.get(f"{fam}+Event", {})
        l1 = res.get(f"{fam}+Event-lag1", {})
        da = "--" if b["DirAcc"] is None else f"{b['DirAcc']:.0f}"
        pla_o = pla.get((fam, "oracle"))
        pla_s = f"{pla_o:.3f}" if pla_o is not None else "--"
        lines.append(
            f"    {fam.replace('PatchTST', 'Patch Transformer (local)')} & {b['MAE']:.3f} & {o.get('MAE', 0):.3f} & {l1.get('MAE', 0):.3f} & "
            f"{pla_s} & {da} & {b['MoveRate']:.0f} \\\\")
    lines += ["    \\bottomrule", "  \\end{tabular}", "\\end{table*}"]
    (PAPER / "tab_forecast.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    t0 = time.time()
    import os
    import tempfile
    ascii_tmp = Path(os.environ.get("GEC_TMPDIR", tempfile.gettempdir())) / "gec_forecast_tmp"
    ascii_tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = str(ascii_tmp)
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    prev = json.loads(RESULTS.read_text(encoding="utf-8"))
    avail = prev["meta"]["availability"]
    reg = model_registry(R.SEQ_LEN)
    key_dates = dataset["key_event_dates"]

    all_results = prev["results"]
    out = {"placebo": [], "feature_ablation": [], "lag_scan": [], "ablation_ci": []}

    # ---------- 1. Placebo ----------
    for name, s in dataset["series"].items():
        periods, y = get_xy(s)
        init = R.WEEKLY_INIT if s["freq"] == "W" else R.MONTHLY_INIT
        win = R.event_windows(key_dates, s["freq"])
        for fam in EVENT_MODELS:
            for setting, mname in (("oracle", f"{fam}+Event"), ("lag1", f"{fam}+Event-lag1")):
                m = reg.get(mname)
                if not m or (m.get("requires") and not avail.get(m["requires"])):
                    continue
                base_r = all_results[name]["models"].get(fam, {})
                real_r = all_results[name]["models"].get(mname, {})
                if real_r.get("status") != "ok":
                    continue
                p_maes, seed_results = [], []
                for seed in PLACEBO_SEEDS:
                    started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
                    pm = placebo_map(s["exog"], periods, seed)
                    met, detail = eval_variant(y, periods, s["exog"], m, init, win,
                                          exog_override=pm, observed_periods={p for p, _ in s["points"]})
                    p_maes.append(met["MAE"])
                    map_hash = hashlib.sha256(json.dumps(pm, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    seed_results.append({"placebo_seed": seed, "model_seed": 42,
                        "mapping_sha256": map_hash, "exog_map": pm, "metrics": met, "folds_detail": detail,
                        "execution": {"started_utc": started_utc, "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                      "job_id": f"placebo/{name}/{fam}/{setting}/seed{seed}"}})
                out["placebo"].append({
                    "series": name, "family": fam, "setting": setting,
                    "base_MAE": base_r.get("MAE"), "real_MAE": real_r.get("MAE"),
                    "placebo_MAE_mean": float(np.mean(p_maes)),
                    "placebo_MAEs": p_maes,
                    "real_minus_placebo": real_r["MAE"] - float(np.mean(p_maes)),
                    "seed_results": seed_results,
                })
                logger.info("placebo %s %s(%s): real=%.3f placebo=%.3f",
                            name, mname, setting, real_r["MAE"], np.mean(p_maes))

    # ---------- 2 & 3. 特征组消融 + 滞后扫描（ARIMA/PatchTST，周度，oracle）----------
    s = dataset["series"]["weekly_overall"]
    periods, y = get_xy(s)
    win = R.event_windows(key_dates, "W")
    feat_names = s["exog_cols"]
    for fam in ("ARIMA", "PatchTST"):
        m = reg[f"{fam}+Event"]
        base_mae = all_results["weekly_overall"]["models"][fam]["MAE"]
        for col, fname in enumerate(feat_names):
            started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
            xm = single_feature_map(s["exog"], periods, col)
            met, detail = eval_variant(y, periods, s["exog"], m, R.WEEKLY_INIT, win,
                                  exog_override=xm, observed_periods={p for p, _ in s["points"]})
            out["feature_ablation"].append({
                "model": fam, "feature": fname, "base_MAE": base_mae,
                **met, "folds_detail": detail, "model_seed": 42,
                "execution": {"started_utc": started_utc, "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                              "job_id": f"feature/weekly_overall/{fam}/{fname}"}})
            logger.info("feat %s+%s: MAE=%.3f (base %.3f)", fam, fname, met["MAE"], base_mae)
        for lag in (0, 1, 2, 4):
            started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
            ml = dict(reg[f"{fam}+Event"], lag=lag)
            if lag in (0, 1):
                model_key = fam + ("+Event" if lag == 0 else "+Event-lag1")
                primary = all_results["weekly_overall"]["models"][model_key]
                detail = copy.deepcopy(primary["folds_detail"])
                met = R.metrics([(d[1], d[2], d[0], d[4], d[3]) for d in detail])
            else:
                met, detail = eval_variant(y, periods, s["exog"], ml, R.WEEKLY_INIT, win,
                                          observed_periods={p for p, _ in s["points"]})
            out["lag_scan"].append({
                "model": fam, "lag": lag, "base_MAE": base_mae,
                **met, "folds_detail": detail, "model_seed": 42,
                "reused_primary": model_key if lag in (0, 1) else None,
                "execution": {"started_utc": started_utc, "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                              "job_id": f"lag/weekly_overall/{fam}/{lag}",
                              "mode": "reuse_primary" if lag in (0, 1) else "new_inference"}})
            logger.info("lag-scan %s lag=%d: MAE=%.3f", fam, lag, met["MAE"])

    # ---------- 6. 全单元 paired bootstrap 95% CI ----------
    for a in prev["event_ablation"]:
        sname, fam = a["series"], a["family"]
        for setting, var in (("oracle", f"{fam}+Event"), ("lag1", f"{fam}+Event-lag1")):
            started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
            rb = all_results[sname]["models"].get(fam)
            rv = all_results[sname]["models"].get(var)
            if not (rb and rv and rb.get("status") == "ok" and rv.get("status") == "ok"):
                continue
            eb = {d[0]: abs(d[1] - d[2]) for d in rb["folds_detail"]}
            ev = {d[0]: abs(d[1] - d[2]) for d in rv["folds_detail"]}
            common = sorted(set(eb) & set(ev))
            diffs = np.array([ev[p] - eb[p] for p in common])
            rng = np.random.default_rng(42)
            boots = rng.choice(diffs, size=(10000, len(diffs)), replace=True).mean(axis=1)
            lo, hi = np.percentile(boots, [2.5, 97.5])
            out["ablation_ci"].append({
                "series": sname, "family": fam, "setting": setting,
                "delta": float(diffs.mean()),
                "ci95": [float(lo), float(hi)], "bootstrap_seed": 42, "bootstrap_draws": 10000,
                "n_folds": len(diffs),
                "execution": {"started_utc": started_utc, "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                              "job_id": f"uncertainty/{sname}/{fam}/{setting}"},
            })

    # ---------- 写回 v4 ----------
    prev["meta"]["version"] = "v4.2"
    prev["meta"]["diagnostics"] = {
        "placebo": f"事件日期随机置换，seed={PLACEBO_SEEDS} 取均值；保持事件数量与强度分布",
        "feature_ablation": "ARIMA/PatchTST 周度 oracle；event_count/strength_sum/up_count/down_count 单特征",
        "lag_scan": "ARIMA/PatchTST 周度 lag∈{0,1,2,4}",
        "ablation_ci": "paired bootstrap 10000 次百分位 95% CI（delta=+Event − base 的逐折 MAE 差）",
        "snaive_note": "SNaive(4) 为周期 4 的季节朴素参照；序列太短无法识别真实季节周期，仅作稳健性参照",
    }
    prev["results"] = all_results
    prev.update(out)
    prev["meta"]["dataset_sha256"] = hashlib.sha256(DATASET.read_bytes()).hexdigest()
    prev["meta"]["implementation_sha256"] = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in ("models.py", "prepare_data.py", "run_experiments.py", "run_diagnostics.py")}
    prev["meta"]["package_versions"] = {name: importlib.metadata.version(name) for name in
        ("numpy", "torch", "statsmodels", "scipy", "prophet", "chronos-forecasting", "transformers")}
    prev["meta"]["diagnostics_elapsed_s"] = time.time()-t0
    RESULTS.write_text(json.dumps(prev, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
    logger.info("forecast_results.json v4 已写（耗时 %.0fs）", time.time() - t0)

    # ---------- 附录 tex + 主表 ----------
    write_appendix_tex(prev)
    write_main_forecast_tex(prev)
    print(f"\nplacebo 汇总：真实优于 placebo（real<placebo）的单元 "
          f"{sum(1 for p in out['placebo'] if p['real_minus_placebo'] < 0)}/{len(out['placebo'])}")


def write_appendix_tex(d):
    """附录表：placebo、月度全量、特征组消融、滞后扫描、全单元 CI。纯 ASCII。"""
    order = ["Naive", "SNaive(4)", "ARIMA", "ARIMA+Event", "ARIMA+Event-lag1",
             "LSTM", "LSTM+Event", "LSTM+Event-lag1", "PatchTST", "PatchTST+Event",
             "PatchTST+Event-lag1", "Chronos-t5-tiny(zs)",
             "Prophet", "Prophet+Event", "Prophet+Event-lag1"]
    res = d["results"]

    def disp(m):
        return m.replace("-lag1", "\\,(lag1)").replace("+Event", "+Evt").replace("PatchTST", "Patch Transformer (local)")

    # A1: 月度全量（三个序列拆成三张表，避免单表超页高）
    order_used = order
    for suffix, name in (("a", "monthly_2024年证"), ("b", "monthly_2025年证"),
                         ("c", "monthly_overall_derived")):
        blk = res[name]
        lines = ["% Auto-generated: src/forecast/run_diagnostics.py (appendix monthly)",
                 "\\begin{table*}[t]", "  \\centering", "  \\footnotesize",
                 f"  \\caption{{Monthly results: {R.SERIES_LABELS[name]} (6 folds)}}",
                 f"  \\label{{tab:app_monthly_{suffix}}}",
                 "  \\begin{tabular}{lccccc}", "    \\toprule",
                 "    Model & MAE & RMSE & MAPE(\\%) & Dir.Acc(\\%) & EventWin-MAE \\\\",
                 "    \\midrule"]
        for m in order_used:
            r = blk["models"].get(m)
            if not r or r.get("status") != "ok":
                continue
            da = "--" if r["DirAcc"] is None else f"{r['DirAcc']:.0f}"
            ev = f"{r['EventWinMAE']:.3f}" if r["EventWinMAE"] is not None else "--"
            lines.append(f"    {disp(m)} & {r['MAE']:.3f} & {r['RMSE']:.3f} & "
                         f"{r['MAPE']:.1f} & {da} & {ev} \\\\")
        lines += ["    \\bottomrule", "  \\end{tabular}", "\\end{table*}"]
        (PAPER / f"tab_app_monthly_{suffix}.tex").write_text("\n".join(lines) + "\n",
                                                             encoding="utf-8")

    # A2: placebo
    lines = ["% Auto-generated: placebo check (appendix Table A2)",
             "\\begin{table*}[t]", "  \\centering", "  \\scriptsize",
             "  \\caption{Placebo check: real vs.\\ date-permuted event features "
             "(mean of 3 seeds). MAE, CNY/certificate}",
             "  \\label{tab:app_placebo}",
             "  \\begin{tabular}{llcccc}", "    \\toprule",
             "    Series & Family & Base & Real & Placebo & Real$-$Placebo \\\\",
             "    \\midrule"]
    for p in d["placebo"]:
        sname = p["series"].replace("monthly_", "M:").replace("weekly_overall", "W:overall") \
                          .replace("年证", "vint.").replace("overall_derived", "overall*")
        lines.append(f"    {sname} & {disp(p['family'])}({p['setting']}) & {p['base_MAE']:.3f} & "
                     f"{p['real_MAE']:.3f} & {p['placebo_MAE_mean']:.3f} & "
                     f"{p['real_minus_placebo']:+.3f} \\\\")
    lines += ["    \\bottomrule", "  \\end{tabular}", "\\end{table*}"]
    (PAPER / "tab_app_placebo.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # A3: 特征组消融 + 滞后扫描（合一表）
    wk_models = res["weekly_overall"]["models"]
    base_note = (f"Feature-group ablation (base MAE: ARIMA "
                 f"{wk_models['ARIMA']['MAE']:.3f}, Patch Transformer (local) {wk_models['PatchTST']['MAE']:.3f})")
    lines = ["% Auto-generated: feature-group ablation and lag scan (appendix Table A3)",
             "\\begin{table*}[t]", "  \\centering", "  \\footnotesize",
             "  \\caption{Feature-group ablation and lag scan (weekly series, oracle features)}",
             "  \\label{tab:app_features}",
             "  \\begin{tabular}{llccc}", "    \\toprule",
             "    Variant & Model & MAE & RMSE & Dir.Acc(\\%) \\\\", "    \\midrule",
             f"    \\multicolumn{{5}}{{l}}{{\\textit{{{base_note}}}}}\\\\"]
    for f in d["feature_ablation"]:
        fname = f["feature"].replace("_", "\\_")
        lines.append(f"    +{fname} & {disp(f['model'])} & {f['MAE']:.3f} & "
                     f"{f['RMSE']:.3f} & {f['DirAcc']:.0f} \\\\")
    lines += ["    \\midrule",
              "    \\multicolumn{5}{l}{\\textit{Lag scan (full features)}}\\\\"]
    for l in d["lag_scan"]:
        lines.append(f"    lag={l['lag']} & {disp(l['model'])} & {l['MAE']:.3f} & "
                     f"{l['RMSE']:.3f} & {l['DirAcc']:.0f} \\\\")
    lines += ["    \\bottomrule", "  \\end{tabular}", "\\end{table*}"]
    (PAPER / "tab_app_features.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # A4: 全单元 CI
    lines = ["% Auto-generated: all-cell deltas with bootstrap 95% CI (appendix Table A4)",
             "\\begin{table*}[t]", "  \\centering", "  \\scriptsize",
             "  \\caption{All 16 ablation cells: per-fold MAE delta (+Event $-$ base) "
             "with paired-bootstrap 95\\% CI}",
             "  \\label{tab:app_ci}",
             "  \\begin{tabular}{lllccc}", "    \\toprule",
             "    Series & Family & Setting & $\\Delta$MAE & 95\\% CI & $n$ \\\\",
             "    \\midrule"]
    for c in d["ablation_ci"]:
        sname = c["series"].replace("monthly_", "M:").replace("weekly_overall", "W:overall") \
                          .replace("年证", "vint.").replace("overall_derived", "overall*")
        lines.append(f"    {sname} & {disp(c['family'])} & {c['setting']} & {c['delta']:+.3f} & "
                     f"[{c['ci95'][0]:+.3f}, {c['ci95'][1]:+.3f}] & {c['n_folds']} \\\\")
    lines += ["    \\bottomrule", "  \\end{tabular}", "\\end{table*}"]
    (PAPER / "tab_app_ci.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("附录 tex 已写: tab_app_monthly/placebo/features/ci")


if __name__ == "__main__":
    main()
