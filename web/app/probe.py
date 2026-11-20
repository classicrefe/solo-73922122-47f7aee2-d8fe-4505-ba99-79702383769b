"""Probe calibration interval analysis.

Given three completed captures of the same reference pulse:

* ``standard``   - reference ("golden") record of the pulse;
* ``calibration``- the same pulse recorded through the probe being checked;
* ``test``       - an unknown-voltage record taken with the SAME probe on the
  SAME channel as the calibration record.

The calibration and standard records share one sample interval. A run of
``pair_count`` consecutive samples (2..64), starting at user supplied sample
indices, is paired in order. Every record keeps its OWN original calibration
(``raw * volt_scale + volt_offset``) when converting samples to volts.

The correction model applied to the whole test record is

    v_corrected = g * v_probe + b

with the gain ``g`` and zero offset ``b`` restricted to closed intervals
(``g > 0``) and, for every paired point,

    | (g * p_i + b) - s_i | <= tolerance

where ``p_i`` is the probe (calibration) voltage and ``s_i`` the standard
voltage. The feasible set is a convex polygon in the (g, b) plane defined by
half-plane constraints. For every in-range test sample the lowest and highest
corrected voltage over that polygon are found by evaluating the linear
objective ``g * v + b`` at every polygon vertex (the extrema of a linear
function over a convex polygon are attained at vertices).

A test sample whose ORIGINAL probe voltage lies outside the voltage span used
for calibration (the voltages of the paired calibration samples) is reported
as out-of-range with empty bounds: extrapolation beyond the validated range is
not quantified.

Everything here is deterministic, side-effect free and uses only the standard
library so the math can be unit tested without a running server.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_PAIRS = 2
MAX_PAIRS = 64


class ProbeAnalysisError(ValueError):
    """The request is invalid (bad records, indices, intervals or parameters)."""


class NoFeasibleParamsError(Exception):
    """The constraints are mutually inconsistent: no (g, b) pair satisfies all."""


@dataclass
class RecordData:
    """Voltage view of one completed capture, converted with its OWN scaling."""

    aid: int
    channel: int
    sample_interval: float
    points: int
    voltages: list[float]  # length == points, volts


@dataclass
class PairPoint:
    index_std: int
    index_cal: int
    standard_v: float
    probe_v: float


@dataclass
class ProbeRequest:
    standard: RecordData
    calibration: RecordData
    test: RecordData
    std_start: int
    cal_start: int
    pair_count: int
    gain_min: float
    gain_max: float
    offset_min: float
    offset_max: float
    tolerance: float


@dataclass
class ProbeResult:
    feasible: bool
    message: str
    pairs: list[PairPoint] = field(default_factory=list)
    vertices: list[tuple[float, float]] = field(default_factory=list)
    probe_min: float | None = None
    probe_max: float | None = None
    rows: list[dict] = field(default_factory=list)  # one entry per test sample
    in_range_count: int = 0
    out_of_range_count: int = 0


# ---------------------------------------------------------------------------
# Half-plane geometry in the (g, b) plane.
#
# Every constraint is written as  a*g + c*b <= d.
# ---------------------------------------------------------------------------

def _feasible_vertices(
    constraints: list[tuple[float, float, float]],
    scale_g: float,
    scale_b: float,
) -> list[tuple[float, float]]:
    """Vertices of the intersection of the given half-planes.

    The polygon is the intersection of half-planes; every vertex is the
    intersection of two boundary lines, so enumerate pairwise line
    intersections and keep points satisfying all constraints. Degenerate
    polygons (a single point or a line segment) are handled naturally because
    vertices of any dimension appear at constraint-line intersections.
    """
    eps = 1e-9 * max(1.0, scale_g, scale_b)
    vertices: list[tuple[float, float]] = []

    def inside(g: float, b: float) -> bool:
        for a, c, d in constraints:
            if a * g + c * b > d + eps:
                return False
        return True

    n = len(constraints)
    for i in range(n):
        a1, c1, d1 = constraints[i]
        for j in range(i + 1, n):
            a2, c2, d2 = constraints[j]
            det = a1 * c2 - a2 * c1
            if abs(det) <= 1e-12 * max(1.0, scale_g) * max(1.0, scale_b):
                continue  # parallel / coincident boundaries: no unique vertex
            g = (d1 * c2 - d2 * c1) / det
            b = (a1 * d2 - a2 * d1) / det
            if not (math.isfinite(g) and math.isfinite(b)):
                continue
            if not inside(g, b):
                continue
            # de-duplicate with a scale-aware absolute tolerance
            duplicate = False
            for g0, b0 in vertices:
                if (
                    abs(g - g0) <= 1e-9 * max(1.0, abs(g0), abs(g), scale_g)
                    and abs(b - b0) <= 1e-9 * max(1.0, abs(b0), abs(b), scale_b)
                ):
                    duplicate = True
                    break
            if not duplicate:
                vertices.append((g, b))
    return vertices


def analyze(req: ProbeRequest) -> ProbeResult:
    """Run the full calibration interval analysis."""
    std, cal, test = req.standard, req.calibration, req.test

    # ---- parameter interval validation ------------------------------------
    if not (math.isfinite(req.gain_min) and math.isfinite(req.gain_max)):
        raise ProbeAnalysisError("增益区间必须为有限数值")
    if not (math.isfinite(req.offset_min) and math.isfinite(req.offset_max)):
        raise ProbeAnalysisError("零偏区间必须为有限数值")
    if not math.isfinite(req.tolerance):
        raise ProbeAnalysisError("电压容差必须为有限数值")
    if req.gain_min <= 0 or req.gain_max <= 0:
        raise ProbeAnalysisError("增益必须为正数（区间端点须 > 0）")
    if req.gain_min > req.gain_max:
        raise ProbeAnalysisError("增益区间下界不得大于上界")
    if req.offset_min > req.offset_max:
        raise ProbeAnalysisError("零偏区间下界不得大于上界")
    if req.tolerance < 0:
        raise ProbeAnalysisError("电压容差必须非负")
    if not (MIN_PAIRS <= req.pair_count <= MAX_PAIRS):
        raise ProbeAnalysisError(f"配对点数须在 {MIN_PAIRS}～{MAX_PAIRS} 之间")

    # ---- index validation --------------------------------------------------
    if req.std_start < 0 or req.cal_start < 0:
        raise ProbeAnalysisError("起始样本号不得为负（样本号从 0 开始）")
    if req.std_start + req.pair_count > std.points:
        raise ProbeAnalysisError(
            f"标准记录样本不足：起始 {req.std_start} + {req.pair_count} 点超出 {std.points} 点"
        )
    if req.cal_start + req.pair_count > cal.points:
        raise ProbeAnalysisError(
            f"探头校验记录样本不足：起始 {req.cal_start} + {req.pair_count} 点超出 {cal.points} 点"
        )

    # ---- record relationship validation -----------------------------------
    if cal.channel != test.channel:
        raise ProbeAnalysisError(
            f"探头校验记录（通道 {cal.channel}）与待测记录（通道 {test.channel}）"
            "必须是同一探头和同一通道"
        )
    interval_eps = 1e-9 * max(1.0, abs(std.sample_interval), abs(cal.sample_interval))
    if abs(std.sample_interval - cal.sample_interval) > interval_eps:
        raise ProbeAnalysisError(
            f"标准记录与探头校验记录采样间隔不同："
            f"{std.sample_interval:g} s vs {cal.sample_interval:g} s"
        )

    pairs: list[PairPoint] = []
    for k in range(req.pair_count):
        i_s = req.std_start + k
        i_c = req.cal_start + k
        pairs.append(
            PairPoint(
                index_std=i_s,
                index_cal=i_c,
                standard_v=std.voltages[i_s],
                probe_v=cal.voltages[i_c],
            )
        )

    # ---- build half-plane constraints in the (g, b) plane -----------------
    # box: gain_min <= g <= gain_max, offset_min <= b <= offset_max
    constraints: list[tuple[float, float, float]] = [
        (1.0, 0.0, req.gain_max),    # g <= gain_max
        (-1.0, 0.0, -req.gain_min),  # -g <= -gain_min (g >= gain_min > 0)
        (0.0, 1.0, req.offset_max),  # b <= offset_max
        (0.0, -1.0, -req.offset_min),
    ]
    # per-pair tolerance: |g*p + b - s| <= tol
    #   g*p + b <= s + tol  and  -g*p - b <= tol - s
    for pp in pairs:
        p, s = pp.probe_v, pp.standard_v
        constraints.append((p, 1.0, s + req.tolerance))
        constraints.append((-p, -1.0, req.tolerance - s))

    scale_p = max(
        (abs(pp.probe_v) for pp in pairs),
        default=1.0,
    )
    scale_s = max((abs(pp.standard_v) for pp in pairs), default=1.0)
    scale_g = max(1.0, abs(req.gain_min), abs(req.gain_max))
    scale_b = max(
        1.0,
        abs(req.offset_min),
        abs(req.offset_max),
        scale_g * scale_p + scale_s,
    )

    vertices = _feasible_vertices(constraints, scale_g, scale_b)
    if not vertices:
        raise NoFeasibleParamsError(
            "约束冲突：增益/零偏区间与全部配对点的容差要求没有共同可行解，"
            "请放宽增益或零偏区间、增大容差，或核对起始样本号与配对记录。"
        )

    # ---- calibrated probe voltage span (paired calibration samples) -------
    probe_voltages = [pp.probe_v for pp in pairs]
    probe_min = min(probe_voltages)
    probe_max = max(probe_voltages)

    # ---- per test-sample corrected voltage envelope -----------------------
    rows: list[dict] = []
    in_range = 0
    out_of_range = 0
    dt = test.sample_interval
    for idx, v in enumerate(test.voltages):
        row: dict = {
            "index": idx,
            "time_s": idx * dt,
            "probe_voltage_v": v,
            "in_range": probe_min <= v <= probe_max,
            "voltage_min_v": None,
            "voltage_max_v": None,
        }
        if row["in_range"]:
            # extrema of the linear form g*v + b over the convex polygon are
            # attained at vertices
            values = [g * v + b for g, b in vertices]
            row["voltage_min_v"] = min(values)
            row["voltage_max_v"] = max(values)
            in_range += 1
        else:
            out_of_range += 1
        rows.append(row)

    return ProbeResult(
        feasible=True,
        message=(
            f"可行：{len(vertices)} 个可行域顶点，{req.pair_count} 个配对点全部满足容差；"
            f"待测 {in_range} 点在探头校验电压范围内，{out_of_range} 点超范围。"
        ),
        pairs=pairs,
        vertices=vertices,
        probe_min=probe_min,
        probe_max=probe_max,
        rows=rows,
        in_range_count=in_range,
        out_of_range_count=out_of_range,
    )
