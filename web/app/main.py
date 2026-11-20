"""FastAPI application: oscilloscope waveform acquisition web service."""
from __future__ import annotations

import os
import struct
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import db, probe
from .manager import AcquisitionManager
from .probe import (
    NoFeasibleParamsError,
    PairPoint,
    ProbeAnalysisError,
    ProbeRequest,
    RecordData,
)

SCOPE_HOST = os.environ.get("SCOPE_HOST", "127.0.0.1")
SCOPE_PORT = int(os.environ.get("SCOPE_PORT", "5025"))
SCOPE_TIMEOUT = float(os.environ.get("SCOPE_TIMEOUT", "5"))
DB_PATH = os.environ.get("DB_PATH", "acquisitions.db")
STATIC_DIR = Path(__file__).parent / "static"

manager: AcquisitionManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    db.init(DB_PATH)
    db.sweep_interrupted()
    manager = AcquisitionManager(SCOPE_HOST, SCOPE_PORT, SCOPE_TIMEOUT)
    manager.start()
    yield
    await manager.stop()


app = FastAPI(title="Oscilloscope Acquisition", lifespan=lifespan)


class AcquireIn(BaseModel):
    channel: int = Field(..., ge=1, le=2)


class ProbeAnalysisIn(BaseModel):
    standard_id: int = Field(..., description="参考脉冲标准记录 ID")
    calibration_id: int = Field(..., description="探头校验记录 ID")
    test_id: int = Field(..., description="同探头同通道待测记录 ID")
    standard_start: int = Field(..., ge=0, description="标准记录起始样本号（从 0 开始）")
    calibration_start: int = Field(..., ge=0, description="校验记录起始样本号（从 0 开始）")
    pair_count: int = Field(..., ge=probe.MIN_PAIRS, le=probe.MAX_PAIRS)
    gain_min: float = Field(..., gt=0, description="正增益闭区间下界（>0）")
    gain_max: float = Field(..., gt=0)
    offset_min: float
    offset_max: float
    tolerance: float = Field(..., ge=0, description="非负电压容差 (V)")


def _load_record(aid: int, role: str) -> RecordData:
    """Load a completed capture and convert its samples to volts using THAT
    record's own original calibration (raw * scale + offset)."""
    row = db.get(aid)
    if row is None:
        raise HTTPException(404, f"{role}记录 #{aid} 不存在")
    if row["status"] != "done" or row["samples"] is None:
        raise HTTPException(
            409, f"{role}记录 #{aid} 尚未完成（当前状态：{row['status']}），不可用于分析"
        )
    points = int(row["points"])
    raw = struct.unpack(f"<{points}h", row["samples"])
    scale = float(row["volt_scale"])
    offset = float(row["volt_offset"])
    voltages = [value * scale + offset for value in raw]
    return RecordData(
        aid=aid,
        channel=int(row["channel"]),
        sample_interval=float(row["sample_interval"]),
        points=points,
        voltages=voltages,
    )


def _build_request(req_in: ProbeAnalysisIn) -> ProbeRequest:
    ids = (req_in.standard_id, req_in.calibration_id, req_in.test_id)
    if len(set(ids)) != 3:
        raise HTTPException(400, "标准记录、探头校验记录、待测记录必须是三条不同的记录")
    standard = _load_record(req_in.standard_id, "标准")
    calibration = _load_record(req_in.calibration_id, "探头校验")
    test = _load_record(req_in.test_id, "待测")
    try:
        return ProbeRequest(
            standard=standard,
            calibration=calibration,
            test=test,
            std_start=req_in.standard_start,
            cal_start=req_in.calibration_start,
            pair_count=req_in.pair_count,
            gain_min=req_in.gain_min,
            gain_max=req_in.gain_max,
            offset_min=req_in.offset_min,
            offset_max=req_in.offset_max,
            tolerance=req_in.tolerance,
        )
    except ProbeAnalysisError as exc:
        raise HTTPException(400, str(exc)) from exc


def _pair_payload(pairs: list[PairPoint]) -> list[dict]:
    return [
        {
            "standard_index": p.index_std,
            "calibration_index": p.index_cal,
            "standard_voltage_v": p.standard_v,
            "probe_voltage_v": p.probe_v,
        }
        for p in pairs
    ]


def public(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k != "samples"}
    out["has_samples"] = row.get("samples") is not None
    return out


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "scope": f"{SCOPE_HOST}:{SCOPE_PORT}",
        "round_timeout_s": SCOPE_TIMEOUT,
    }


@app.get("/api/acquisitions")
async def list_acquisitions(limit: int = 50):
    return [public(r) for r in db.list_recent(limit)]


@app.post("/api/acquisitions", status_code=201)
async def create_acquisition(req: AcquireIn):
    aid = manager.submit(req.channel)
    return public(db.get(aid))


@app.get("/api/acquisitions/{aid}")
async def get_acquisition(aid: int):
    row = public(db.get(aid))
    if row is None:
        raise HTTPException(404, "acquisition not found")
    return row


@app.post("/api/acquisitions/{aid}/cancel")
async def cancel_acquisition(aid: int):
    row = manager.cancel(aid)
    if row is None:
        raise HTTPException(404, "acquisition not found")
    return public(row)


@app.get("/api/acquisitions/{aid}/csv")
async def acquisition_csv(aid: int):
    row = db.get(aid)
    if row is None:
        raise HTTPException(404, "acquisition not found")
    # Only a fully received block (parameters + samples stored together) may
    # be downloaded.
    if row["status"] != "done" or row["samples"] is None:
        raise HTTPException(409, "capture not complete; CSV unavailable")
    points = int(row["points"])
    raw = struct.unpack(f"<{points}h", row["samples"])
    dt = float(row["sample_interval"])
    scale = float(row["volt_scale"])
    offset = float(row["volt_offset"])
    lines = ["time_s,voltage_v"]
    append = lines.append
    for i, value in enumerate(raw):
        append(f"{i * dt:.9e},{value * scale + offset:.6e}")
    filename = f"acquisition_{aid}_ch{row['channel']}.csv"
    return Response(
        "\n".join(lines) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/probe-analysis")
async def probe_analysis(req_in: ProbeAnalysisIn):
    """Feasible-parameter envelope of corrected test voltages.

    A 200 response always carries a ``feasible`` flag. When the constraints
    conflict the flag is false with an explanatory message and no rows, so the
    UI can clear any previous result.
    """
    req = _build_request(req_in)
    try:
        result = probe.analyze(req)
    except ProbeAnalysisError as exc:
        raise HTTPException(400, str(exc)) from exc
    except NoFeasibleParamsError as exc:
        return {
            "feasible": False,
            "message": str(exc),
            "pairs": [],
            "vertices": [],
            "probe_voltage_range_v": None,
            "in_range_count": 0,
            "out_of_range_count": 0,
            "rows": [],
        }
    return {
        "feasible": True,
        "message": result.message,
        "pairs": _pair_payload(result.pairs),
        "vertices": [{"gain": g, "offset": b} for g, b in result.vertices],
        "probe_voltage_range_v": [result.probe_min, result.probe_max],
        "in_range_count": result.in_range_count,
        "out_of_range_count": result.out_of_range_count,
        "rows": result.rows,
    }


@app.post("/api/probe-analysis/csv")
async def probe_analysis_csv(req_in: ProbeAnalysisIn):
    """Same analysis as a downloadable CSV. Conflicting constraints -> 409 so
    no stale/empty file is saved."""
    req = _build_request(req_in)
    try:
        result = probe.analyze(req)
    except ProbeAnalysisError as exc:
        raise HTTPException(400, str(exc)) from exc
    except NoFeasibleParamsError as exc:
        raise HTTPException(409, str(exc)) from exc
    lines = [
        "sample_index,time_s,voltage_min_v,voltage_max_v,status",
    ]
    append = lines.append
    for row in result.rows:
        if row["in_range"]:
            append(
                f"{row['index']},{row['time_s']:.9e},"
                f"{row['voltage_min_v']:.6e},{row['voltage_max_v']:.6e},in_range"
            )
        else:
            # Out of the validated probe voltage span: bounds left empty.
            append(f"{row['index']},{row['time_s']:.9e},,,out_of_range")
    filename = (
        f"probe_analysis_std{req_in.standard_id}_cal{req_in.calibration_id}"
        f"_test{req_in.test_id}.csv"
    )
    return Response(
        "\n".join(lines) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
