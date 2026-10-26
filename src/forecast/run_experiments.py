# -*- coding: utf-8 -*-
"""v4.2 CPU rolling-origin benchmark. Weekly grid: causal forward fill of past missing history; score only observed targets (23 folds). Monthly: 6 folds each. Event neural windows use identical target-aligned y-history and next-x layouts during fit/inference. Values and folds are saved without prediction rounding. Undefined direction accuracy is JSON null. The oracle and lag1 settings are retrospective report-period feature assumptions, not deployable real-time backtests."""
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from models import model_registry, availability, chronos_asset_metadata  # noqa: E402

logger = logging.getLogger("forecast_eval")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)

DATASET = ROOT / "data" / "forecast_dataset_v4_2.json"
RESULTS = ROOT / "data" / "forecast_results_v4_2.json"
TEX = ROOT / "03_论文投稿" / "paper" / "tab_forecast.tex"

SEQ_LEN = 8
WEEKLY_INIT, MONTHLY_INIT = 26, 10


def to_weekly(points):
    """Causal weekly grid: forward-fill past values; flags mark unobserved weeks."""
    pts = {p: v for p, v in points}
    d0 = dt.date.fromisoformat(points[0][0])
    d1 = dt.date.fromisoformat(points[-1][0])
    periods, y, flags = [], [], []
    d = d0
    while d <= d1:
        k = d.isoformat()
        if k in pts:
            v, f = pts[k], False
        else:
            prev = max(p for p in pts if p < k)
            v, f = pts[prev], True
        periods.append(k)
        y.append(v)
        flags.append(f)
        d += dt.timedelta(days=7)
    return periods, y, flags


def metrics(folds):
    """folds: [(actual, pred, period, in_event_window)]"""
    act = np.array([f[0] for f in folds], dtype=float)
    pred = np.array([f[1] for f in folds], dtype=float)
    mae = float(np.mean(np.abs(act - pred)))
    rmse = float(np.sqrt(np.mean((act - pred) ** 2)))
    mape = float(np.mean(np.abs((act - pred) / np.clip(act, 1e-6, None))) * 100)
    # 方向准确率（tie-aware）：pred 与上期实际值相等为 tie，不计对错、单独计数；
    # 同时报 MoveRate = pred≠上期 的比例（衡量模型"敢动"程度）
    dir_ok, dir_n, n_ties, n_move = 0, 0, 0, 0
    for f in folds:
        prev, a, p = float(f[4]), float(f[0]), float(f[1])
        if abs(p - prev) < 1e-9:
            n_ties += 1
            continue
        n_move += 1
        if abs(a - prev) < 1e-9:
            continue
        dir_n += 1
        if (a - prev) * (p - prev) > 0:
            dir_ok += 1
    dir_acc = dir_ok / dir_n * 100 if dir_n else None
    move_rate = n_move / len(folds) * 100 if folds else None
    ev = [f for f in folds if f[3]]
    ev_mae = float(np.mean([abs(float(f[0]) - float(f[1])) for f in ev])) if ev else None
    return {"MAE": mae, "RMSE": rmse, "MAPE": mape,
            "DirAcc": dir_acc, "MoveRate": move_rate,
            "n_ties": n_ties,
            "dir_correct": dir_ok, "dir_n": dir_n,
            "EventWinMAE": ev_mae,
            "n_folds": len(folds), "n_event_folds": len(ev),
            "fold_mae_std": float(np.std(np.abs(act - pred)))}


def event_windows(dates, freq):
    """关键事件 ±1 期的目标期集合。"""
    win = set()
    for ds in dates:
        d = dt.date.fromisoformat(ds)
        if freq == "W":
            ws = d - dt.timedelta(days=d.weekday())
            for k in (-7, 0, 7):
                win.add((ws + dt.timedelta(days=k)).isoformat())
        else:
            for delta in (-1, 0, 1):
                m = d.month + delta
                y = d.year + (m - 1) // 12
                m = (m - 1) % 12 + 1
                win.add(f"{y}-{m:02d}")
    return win


def run_series(name, s, key_dates, reg, avail):
    freq = s["freq"]
    if freq == "W":
        periods, y, flags = to_weekly(s["points"])
    else:
        periods, y, flags = [p for p, _ in s["points"]], [v for _, v in s["points"]], []
    exog_map = s["exog"]
    win = event_windows(key_dates, freq)
    init = WEEKLY_INIT if freq == "W" else MONTHLY_INIT
    results = {}
    for mname, m in reg.items():
        started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
        if m.get("requires") and not avail.get(m["requires"]):
            results[mname] = {"status": f"unavailable({m['requires']}未安装)"}
            continue
        folds, information_trace = [], []
        try:
            lag = m.get("lag", 0)
            for t in range(init, len(y)):
                # Missing target values are never evaluated or treated as real.
                if flags and flags[t]:
                    continue
                train_y = y[:t]
                # lag1：事件特征整体滞后一期（real-time）；oracle：用目标期特征
                src = lambda i: exog_map.get(periods[i-lag], [0, 0, 0, 0]) if i-lag >= 0 else [0, 0, 0, 0]
                tx = src(t)
                train_x = ([src(i) for i in range(t)] if m["exog"] else None)
                if m.get("prophet"):
                    from models import prophet_base, prophet_exog
                    fn = prophet_exog if m["exog"] else prophet_base
                    pred = fn(train_y, train_x, tx, periods[:t], periods[t])
                elif m["exog"]:
                    pred = m["fn"](train_y, train_x, tx)
                else:
                    pred = m["fn"](train_y, None, None)
                folds.append((y[t], pred, periods[t], periods[t] in win, y[t - 1]))
                information_trace.append({"target_period": periods[t], "train_end_period": periods[t-1],
                    "target_observed": True, "event_source_period": periods[t-lag] if m["exog"] and t-lag >= 0 else None,
                    "event_features": tx if m["exog"] else None})
            r = metrics(folds)
            r["status"] = "ok"
            r["fold_maes"] = [abs(float(f[0]) - float(f[1])) for f in folds]
            # 每折明细（敏感性分析与配对检验用）
            r["folds_detail"] = [[f[2], float(f[0]), float(f[1]), float(f[4]), bool(f[3])] for f in folds]
            r["information_trace"] = information_trace
            r["execution"] = {"started_utc": started_utc,
                "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "model_seed": 42,
                "job_id": f"primary/{name}/{mname}"}
            results[mname] = r
            logger.info("%s %s: MAE=%.3f RMSE=%.3f DirAcc=%s%% (%d 折)",
                        name, mname, r["MAE"], r["RMSE"], r["DirAcc"], r["n_folds"])
        except Exception as e:
            results[mname] = {"status": f"failed({type(e).__name__}: {str(e)[:120]})"}
            logger.warning("%s %s 失败: %s", name, mname, e)
    meta = {"freq": freq, "n_points": len(y), "periods": [periods[0], periods[-1]],
            "init_train": init, "interpolated": [p for p, f in zip(periods, flags) if f],
            "y": y, "period_list": periods, "imputation": "causal_ffill" if freq == "W" else "none",
            "evaluation_targets": "observed_only", "n_observed": len(s["points"])}
    return meta, results


SERIES_LABELS = {
    "weekly_overall": "Weekly overall average deal price (GZ exchange, 52 points)",
    "monthly_2024年证": "Monthly average deal price, 2024 vintage (NEA bulletin, 16 points)",
    "monthly_2025年证": "Monthly average deal price, 2025 vintage (NEA bulletin, 16 points)",
    "monthly_overall_derived": "Monthly overall average (vintage volume-weighted, derived; 16 points)",
}


def sensitivity_excl_interpolated(blk):
    """周度敏感性：排除落在测试区间的插值目标周，重算全部模型指标。"""
    interp = set(blk["meta"]["interpolated"])
    out = {}
    for m, r in blk["models"].items():
        if r.get("status") != "ok":
            continue
        folds = [(d[1], d[2], d[0], d[4], d[3]) for d in r["folds_detail"]
                 if d[0] not in interp]
        if not folds:
            continue
        out[m] = metrics(folds)
        out[m]["n_folds"] = len(folds)
    return out


def paired_tests(all_results):
    """对 3 个数值改善单元做 per-fold 配对检验（paired bootstrap + DM）。

    折数仅 6，检验功效低，p 值仅供参考（已在报告中注明）。
    """
    cells = [(name, family, family + ("+Event" if setting == "oracle" else "+Event-lag1"), setting)
             for name in all_results for family in ("ARIMA", "LSTM", "PatchTST", "Prophet")
             for setting in ("oracle", "lag1")]
    tests = []
    for sname, base, var, label in cells:
        rng = np.random.default_rng(42)
        rb = all_results[sname]["models"].get(base)
        rv = all_results[sname]["models"].get(var)
        if not (rb and rv and rb.get("status") == "ok" and rv.get("status") == "ok"):
            continue
        eb = {d[0]: abs(d[1] - d[2]) for d in rb["folds_detail"]}
        ev = {d[0]: abs(d[1] - d[2]) for d in rv["folds_detail"]}
        common = sorted(set(eb) & set(ev))
        diffs = np.array([ev[p] - eb[p] for p in common])  # <0 = variant 更好
        n = len(diffs)
        # paired bootstrap（百分位法，10000 次）
        boots = rng.choice(diffs, size=(10000, n), replace=True).mean(axis=1)
        p_boot = min(1.0, 2 * min(float((boots >= 0).mean()), float((boots <= 0).mean())))
        # Diebold-Mariano（h=1，Harvey 小样本修正，t(n-1) 近似）
        from scipy import stats as sps
        d = -diffs  # >0 = variant 更好
        mean_d = d.mean()
        var_d = d.var(ddof=1)
        dm = mean_d / np.sqrt(var_d / n) if var_d > 0 else None
        dm_h = dm * np.sqrt((n - 1) / n) if dm is not None else None
        p_dm = 2 * float(sps.t.sf(abs(dm_h), df=n - 1)) if n > 1 and dm_h is not None else None
        disp = var.replace("-lag1", "\\,(lag1)").replace("+Event", "+Evt")
        tests.append({"cell": f"{disp} @ {sname.replace('monthly_', '').replace('年证',' vintage')} ({label})",
                      "series": sname, "family": base, "setting": label, "n_folds": n,
                      "mean_abs_diff": float(diffs.mean()),
                      "p_paired_bootstrap": p_boot,
                      "dm_stat_harvey": float(dm_h) if dm_h is not None else None,
                      "p_dm_t": p_dm, "dm_degenerate": bool(var_d <= 0),
                      "power_note": f"n={n}; exploratory iid fold approximation; overlapping histories and multiple cells limit inference"})
    return tests




def main():
    t0 = time.time()
    # prophet/cmdstan 在含中文的用户临时目录下解析 Stan csv 失败，改指 ASCII 临时目录
    import os
    import tempfile
    ascii_tmp = Path(os.environ.get("GEC_TMPDIR", tempfile.gettempdir())) / "gec_forecast_tmp"
    ascii_tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = str(ascii_tmp)
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    key_dates = dataset["key_event_dates"]
    avail = availability()
    logger.info("可用性: %s", avail)
    reg = model_registry(SEQ_LEN)

    all_results = {}
    for name, s in dataset["series"].items():
        meta, res = run_series(name, s, key_dates, reg, avail)
        all_results[name] = {"meta": meta, "models": res}

    # 16 组事件消融明细（4 序列 × 4 事件增强模型；oracle 与 lag1 两 setting）
    families = [("ARIMA", "ARIMA+Event", "ARIMA+Event-lag1"),
                ("LSTM", "LSTM+Event", "LSTM+Event-lag1"),
                ("PatchTST", "PatchTST+Event", "PatchTST+Event-lag1"),
                ("Prophet", "Prophet+Event", "Prophet+Event-lag1")]
    ablation = []
    for sname, blk in all_results.items():
        for base, oracle, lag1 in families:
            b, o, l = (blk["models"].get(base), blk["models"].get(oracle),
                       blk["models"].get(lag1))
            if not all(r and r.get("status") == "ok" for r in (b, o, l)):
                continue
            ablation.append({
                "series": sname, "family": base,
                "base_MAE": b["MAE"], "oracle_MAE": o["MAE"], "lag1_MAE": l["MAE"],
                "oracle_delta": o["MAE"] - b["MAE"],
                "lag1_delta": l["MAE"] - b["MAE"],
                "oracle_improved": o["MAE"] < b["MAE"],
                "lag1_improved": l["MAE"] < b["MAE"],
            })

    weekly_sens = sensitivity_excl_interpolated(all_results["weekly_overall"])
    tests = paired_tests(all_results)

    payload = {
        "meta": {
            "version": "v4.2",
            "design": "rolling-origin h=1；周度52点grid（47实测+5因果前向填充）；"
                      "init=26；仅实测目标评估23折；"
                      "月度各 16 点 init=10 → 6 折（事件窗口折仅 3 折）",
            "event_settings": "oracle=same-period retrospective feature assumption; lag1=one-period event proxy; actual report publication timestamps unavailable",
            "event_feature_note": "72条gold事件+4条curated（移除与gold文档9/41重复的两条curated）；"
                                  "事件覆盖不完整，结果为描述性结果",
            "dir_acc_note": "Tie-aware DirAcc; Naive is undefined/null because all predictions are ties; dir_n and dir_correct saved",
            "interpolated_weeks": sorted({p for blk in all_results.values()
                                          for p in blk["meta"]["interpolated"]}),
            "interpolated_in_test": sorted(
                set(all_results["weekly_overall"]["meta"]["interpolated"])
                & {d[0] for d in all_results["weekly_overall"]["models"]["ARIMA"]["folds_detail"]}),
            "availability": avail,
            "torch_device": "cpu（3060 6GB 可用，但小模型 CPU 足够且可复现）",
            "seed": 42,
            "preprocessing": "causal_ffill; observed targets only; full weekly-grid exog",
            "legacy_interpolated_keys": "Compatibility keys only: v4.2 lists forward-filled historical grid values, not interpolation",
            "neural_exog_alignment": "target y_j uses rows [y_i,x_(i+1)] for i=j-8..j-1, identical fit/inference",
            "torch_threads": 1,
            "chronos_assets": chronos_asset_metadata(),
            "model_label_note": "PatchTST key denotes a local patch Transformer, not the official PatchTST implementation",
        },
        "event_ablation": ablation,
        "weekly_sensitivity_excl_interpolated": weekly_sens,
        "significance_tests": tests,
        "results": all_results,
        "elapsed_s": round(time.time() - t0, 1),
    }
    RESULTS.write_text(json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
    # Canonical TeX is generated by run_diagnostics only after all expected jobs.
    logger.info("结果: %s；表格: %s（耗时 %.0fs）", RESULTS, TEX, time.time() - t0)

    # 控制台汇总
    print("\n===== 周度敏感性（排除测试区间插值周，23 折）=====")
    for m in ("ARIMA", "Chronos-t5-tiny(zs)", "PatchTST", "Naive", "LSTM", "Prophet"):
        r0 = all_results["weekly_overall"]["models"].get(m, {})
        r1 = weekly_sens.get(m, {})
        if r0.get("status") == "ok" and r1:
            print(f"  {m:<22} MAE {r0['MAE']:.3f} -> {r1['MAE']:.3f} | "
                  f"DirAcc {r0['DirAcc']} -> {r1['DirAcc']}")
    print(f"\n===== 探索性配对检验（全部 {len(tests)} 单元）=====")
    for t in tests:
        print(f"  {t['cell']}: bootstrap p={t['p_paired_bootstrap']}, "
              f"DM(Harvey)={t['dm_stat_harvey']}, p={t['p_dm_t']}")

    # 控制台汇总
    print("\n===== 事件消融明细（16 组 × oracle/lag1）=====")
    n_oi = sum(1 for a in ablation if a["oracle_improved"])
    n_li = sum(1 for a in ablation if a["lag1_improved"])
    for a in ablation:
        print(f"  {a['series']:<26} {a['family']:<9} base={a['base_MAE']:.3f} "
              f"oracle={a['oracle_MAE']:.3f}({a['oracle_delta']:+.3f}) "
              f"lag1={a['lag1_MAE']:.3f}({a['lag1_delta']:+.3f})")
    print(f"MAE 改善组数：oracle {n_oi}/16，lag1 {n_li}/16")

    # 控制台汇总
    print("\n===== 汇总（MAE，元/个）=====")
    for name, blk in all_results.items():
        print(f"-- {name} ({blk['meta']['n_points']} 点, {blk['meta']['init_train']}+ 滚动, "
              f"{next(iter(blk['models'].values())).get('n_folds', '?')} 折)")
        for m, r in blk["models"].items():
            if r.get("status") == "ok":
                print(f"   {m:<24} MAE={r['MAE']:.3f} RMSE={r['RMSE']:.3f} "
                      f"MAPE={r['MAPE']:.1f}% DirAcc={r['DirAcc']}% "
                      f"Move={r['MoveRate']:.0f}% EvWin={r['EventWinMAE']}")
            else:
                print(f"   {m:<24} {r['status']}")


if __name__ == "__main__":
    main()
