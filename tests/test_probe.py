#!/usr/bin/env python3
"""Unit tests for the probe calibration interval analysis (pure math).

Runs without a server or third-party packages:

    python3 tests/test_probe.py
"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "web"))

from app.probe import (  # noqa: E402
    MAX_PAIRS,
    MIN_PAIRS,
    NoFeasibleParamsError,
    ProbeAnalysisError,
    ProbeRequest,
    RecordData,
    analyze,
)

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label, flush=True)
    if not cond:
        failures.append(label)


def make_record(aid: int, channel: int, interval: float, voltages: list[float]) -> RecordData:
    return RecordData(
        aid=aid,
        channel=channel,
        sample_interval=interval,
        points=len(voltages),
        voltages=list(voltages),
    )


def linear_record(aid: int, channel: int, interval: float, n: int, g: float, b: float,
                  base: list[float]) -> RecordData:
    """Probe-side voltages of `base` under correction v = g*p + b."""
    return make_record(aid, channel, interval, [(v - b) / g for v in base])


def base_pulse(n: int = 32) -> list[float]:
    return [1.0 + 0.25 * i for i in range(n)]  # 1.0 .. 8.75 V


def build(std_v, cal_v, test_v, *, std_start=0, cal_start=0, pair_count=8,
          g_lo=0.5, g_hi=2.0, b_lo=-5.0, b_hi=5.0, tol=0.1,
          interval=1e-6, test_interval=2e-6, test_channel=1):
    std = make_record(1, 2, interval, std_v)
    cal = make_record(2, test_channel, interval, cal_v)
    test = make_record(3, test_channel, test_interval, test_v)
    return ProbeRequest(
        standard=std, calibration=cal, test=test,
        std_start=std_start, cal_start=cal_start, pair_count=pair_count,
        gain_min=g_lo, gain_max=g_hi, offset_min=b_lo, offset_max=b_hi,
        tolerance=tol,
    )


def main() -> int:
    n = 32
    base = base_pulse(n)
    g_true, b_true = 1.25, -0.4

    # ---- 1. exact linear relation, tolerance 0 -> single feasible point ----
    print("[1] exact correction, zero tolerance -> single feasible point")
    cal_v = [(v - b_true) / g_true for v in base]
    req = build(base, cal_v, cal_v[:10], pair_count=8,
                g_lo=0.5, g_hi=2.0, b_lo=-5.0, b_hi=5.0, tol=0.0)
    res = analyze(req)
    check(res.feasible, "analysis feasible")
    check(len(res.vertices) >= 1, "at least one feasible vertex")
    g_ok = all(abs(g - g_true) <= 1e-7 for g, _ in res.vertices)
    b_ok = all(abs(b - b_true) <= 1e-7 for _, b in res.vertices)
    check(g_ok and b_ok, f"all vertices equal true (g={g_true}, b={b_true})")
    # envelope collapses onto the exact corrected value
    row0 = res.rows[0]
    expected0 = g_true * cal_v[0] + b_true
    check(
        math.isclose(row0["voltage_min_v"], expected0, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(row0["voltage_max_v"], expected0, rel_tol=1e-9, abs_tol=1e-9),
        "single feasible parameter still produces equal min/max bounds",
    )

    # ---- 2. wide boxes give a genuine interval containing the true value ---
    print("[2] finite tolerance -> intervals bracket the true correction")
    in_span = cal_v[2:7]  # strictly inside the paired calibration span
    req = build(base, cal_v, in_span, pair_count=8,
                g_lo=1.0, g_hi=1.5, b_lo=-1.0, b_hi=0.5, tol=0.2)
    res = analyze(req)
    check(res.feasible, "feasible with wide boxes")
    check(len(res.vertices) >= 3, f"polygon has >= 3 vertices (got {len(res.vertices)})")
    check(all(r["in_range"] for r in res.rows), "all test samples inside calibrated span")
    ok_bracket = True
    for r in res.rows:
        exp = g_true * r["probe_voltage_v"] + b_true
        if not (r["voltage_min_v"] - 1e-9 <= exp <= r["voltage_max_v"] + 1e-9):
            ok_bracket = False
            break
        if r["voltage_min_v"] > r["voltage_max_v"]:
            ok_bracket = False
            break
    check(ok_bracket, "every [min,max] bracket contains the true corrected value")

    # ---- 3. every feasible vertex satisfies all pairing tolerance checks ---
    print("[3] all vertices satisfy every pairing constraint")
    ok_all = True
    for g, b in res.vertices:
        for p in res.pairs:
            if abs(g * p.probe_v + b - p.standard_v) > 0.2 + 1e-7:
                ok_all = False
    check(ok_all, "|g*p+b - s| <= tolerance at every vertex/pair")
    check(all(res.vertices[i][0] >= 1.0 - 1e-9 for i in range(len(res.vertices))),
          "vertices respect gain lower bound (positive gain only)")

    # ---- 4. conflicting constraints -> NoFeasibleParamsError ---------------
    print("[4] conflicting gain box -> no feasible parameter")
    req = build(base, cal_v, cal_v[:10], pair_count=8,
                g_lo=2.0, g_hi=3.0, b_lo=-0.01, b_hi=0.01, tol=0.0)
    raised = False
    try:
        analyze(req)
    except NoFeasibleParamsError:
        raised = True
    check(raised, "NoFeasibleParamsError raised on conflict")

    # ---- 4b. tolerance too small for a perturbed calibration ---------------
    print("[4b] perturbed calibration with tight tolerance -> conflict")
    perturbed = [v + (0.5 if i % 2 == 0 else -0.5) for i, v in enumerate(cal_v)]
    req = build(base, perturbed, perturbed[:10], pair_count=8, tol=0.01)
    raised = False
    try:
        analyze(req)
    except NoFeasibleParamsError:
        raised = True
    check(raised, "tight tolerance rejects noisy pairing")
    # the same data becomes feasible once the tolerance covers the residual
    res2 = analyze(build(base, perturbed, perturbed[:10], pair_count=8, tol=2.0))
    check(res2.feasible, "larger tolerance restores feasibility")

    # ---- 5. out-of-range test samples get empty bounds ---------------------
    print("[5] out-of-range samples flagged with empty bounds")
    pmin, pmax = min(cal_v[:8]), max(cal_v[:8])
    test_v = [pmin, (pmin + pmax) / 2, pmax, pmin - 1.0, pmax + 1.0]
    req = build(base, cal_v, test_v, pair_count=8, tol=0.2,
                g_lo=1.0, g_hi=1.5, b_lo=-1.0, b_hi=0.5)
    res = analyze(req)
    check(len(res.rows) == 5, "one result row per test sample")
    check(res.rows[0]["in_range"] and res.rows[1]["in_range"] and res.rows[2]["in_range"],
          "samples within/on the calibrated span are in-range (boundary inclusive)")
    check(not res.rows[3]["in_range"] and not res.rows[4]["in_range"],
          "samples beyond the calibrated span are out-of-range")
    check(res.rows[3]["voltage_min_v"] is None and res.rows[3]["voltage_max_v"] is None,
          "out-of-range bounds are empty (None)")
    check(res.out_of_range_count == 2 and res.in_range_count == 3,
          "coverage counters correct")
    check(math.isclose(res.probe_min, pmin, rel_tol=1e-12)
          and math.isclose(res.probe_max, pmax, rel_tol=1e-12),
          "probe span taken from paired calibration samples")

    # ---- 6. each record keeps its OWN original scaling ---------------------
    print("[6] original per-record calibration is applied to raw samples")
    raw_std = [100, 200, 300, 400]
    raw_cal = [10, 20, 30, 40]
    std = RecordData(10, 1, 1e-6, 4, [v * 0.1 + 0.0 for v in raw_std])      # 10..40 V
    cal = RecordData(11, 2, 1e-6, 4, [v * 1.0 + 5.0 for v in raw_cal])       # 15..45 V
    test = RecordData(12, 2, 1e-6, 4, [v * 1.0 + 5.0 for v in raw_cal])
    req = ProbeRequest(std, cal, test, 0, 0, 4, 0.5, 2.0, -10.0, 10.0, 50.0)
    res = analyze(req)
    check(res.feasible, "feasible across differently scaled records")
    check(math.isclose(res.pairs[0].standard_v, 10.0)
          and math.isclose(res.pairs[0].probe_v, 15.0),
          "paired voltages use each record's own scale/offset")

    # ---- 7. validation errors ----------------------------------------------
    print("[7] request validation")
    cal_ch2 = make_record(2, 2, 1e-6, cal_v)
    test_ch1 = make_record(3, 1, 1e-6, cal_v[:10])
    req = build(base, cal_v, cal_v[:10], test_channel=2)
    req.test = test_ch1
    try:
        analyze(req)
        check(False, "channel mismatch rejected")
    except ProbeAnalysisError as e:
        check("同一探头" in str(e) or "通道" in str(e), f"channel mismatch rejected ({e})")

    req = build(base, cal_v, cal_v[:10])
    req.standard = make_record(1, 2, 5e-6, base)  # different interval
    try:
        analyze(req)
        check(False, "interval mismatch rejected")
    except ProbeAnalysisError as e:
        check("采样间隔" in str(e), f"interval mismatch rejected ({e})")

    for kwargs, needle in [
        (dict(g_lo=-1.0, g_hi=1.0), "增益"),
        (dict(g_lo=1.5, g_hi=1.0), "增益"),
        (dict(b_lo=1.0, b_hi=0.0), "零偏"),
        (dict(tol=-0.1), "容差"),
        (dict(pair_count=1), "配对"),
        (dict(pair_count=MAX_PAIRS + 1), "配对"),
    ]:
        req = build(base, cal_v, cal_v[:10], **kwargs)
        try:
            analyze(req)
            check(False, f"rejected: {needle}")
        except ProbeAnalysisError as e:
            check(needle in str(e), f"rejected bad {needle} ({e})")

    req = build(base, cal_v, cal_v[:10], std_start=30, pair_count=8)
    try:
        analyze(req)
        check(False, "start index overrun rejected")
    except ProbeAnalysisError as e:
        check("样本不足" in str(e), f"start overrun rejected ({e})")

    req = build(base, cal_v, cal_v[:10], std_start=-1)
    try:
        analyze(req)
        check(False, "negative start rejected")
    except ProbeAnalysisError as e:
        check("起始样本号" in str(e), f"negative start rejected ({e})")

    # ---- 8. bounds use the TEST record time base ---------------------------
    print("[8] test record may use its own sample interval for time column")
    req = build(base, cal_v, cal_v[:5], pair_count=4, test_interval=4e-6)
    res = analyze(req)
    check(math.isclose(res.rows[3]["time_s"], 3 * 4e-6, rel_tol=1e-12),
          "time column follows the test record's sample interval")

    # ---- 9. MIN_PAIRS boundary and all-pairs coverage ----------------------
    print("[9] minimum pair count works")
    req = build(base, cal_v, cal_v[:4], pair_count=MIN_PAIRS, tol=0.05)
    res = analyze(req)
    check(res.feasible and len(res.pairs) == MIN_PAIRS,
          f"{MIN_PAIRS}-point pairing accepted")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  - " + f)
        return 1
    print("ALL PROBE UNIT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
