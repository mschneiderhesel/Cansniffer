"""FastAPI-App: REST + WebSocket für Live-Frames."""
from __future__ import annotations

import asyncio
import json
import os
import queue
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analysis
from .capture import Capture
from .db import Store

DB_PATH = os.environ.get("CANSNIFFER_DB", "cansniffer.db")
STATIC = Path(__file__).parent / "static"

store = Store(DB_PATH)
capture = Capture(store)
app = FastAPI(title="CAN Sniffer")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# --- Quelle & Aufnahme ---------------------------------------------------

class OpenReq(BaseModel):
    interface: str = "slcan"      # slcan | socketcan | gs_usb | pcan | sim
    channel: str = "/dev/tty.usbmodem14101"
    bitrate: int = 250000
    listen_only: bool = True


@app.post("/api/source/open")
def source_open(req: OpenReq):
    try:
        capture.open(req.interface, req.channel, req.bitrate, req.listen_only)
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    return status()


@app.post("/api/source/close")
def source_close():
    capture.stop_recording()
    capture.close()
    return status()


@app.get("/api/status")
def status():
    return {
        "open": capture.is_open(),
        "source": capture.source_desc,
        "recording": capture.recording,
        "session_id": capture.session_id,
        "frames": capture.frame_count,
        "ids": len(capture.stats),
        "error": capture.error,
        "sim": capture.sim is not None,
    }


class RecReq(BaseModel):
    name: str
    note: str = ""


@app.post("/api/record/start")
def record_start(req: RecReq):
    if not capture.is_open():
        raise HTTPException(400, "Keine Quelle geöffnet")
    sid = capture.start_recording(req.name, req.note)
    return {"session_id": sid}


@app.post("/api/record/stop")
def record_stop():
    capture.stop_recording()
    return status()


@app.get("/api/sessions")
def sessions():
    return store.sessions()


# --- Live-Statistik -------------------------------------------------------

@app.get("/api/live/ids")
def live_ids():
    rows = []
    for cid, st in sorted(capture.stats.items()):
        rows.append({"id": cid, "id_hex": f"{cid:03X}", "count": st["count"], "dlc": st["dlc"],
                     "last": st["last"],
                     "min_dt_ms": round(1000 * st["min_dt"], 1) if st["min_dt"] else None})
    return rows


@app.post("/api/live/reset")
def live_reset():
    capture.stats.clear()
    capture.frame_count = 0
    capture.recent.clear()
    return {"ok": True}


# --- Marker ---------------------------------------------------------------

class MarkerReq(BaseModel):
    label: str
    head: int | None = None
    key: int | None = None
    note: str = ""
    session_id: int | None = None


@app.post("/api/markers")
def add_marker(req: MarkerReq):
    sid = req.session_id or capture.session_id
    if sid is None:
        raise HTTPException(400, "Keine laufende Aufnahme")
    m = store.add_marker(sid, req.label, req.head, req.key, req.note)
    if capture.sim is not None and req.head and req.key:
        capture.sim.press(req.head, req.key)
    return m


@app.get("/api/markers/{session_id}")
def list_markers(session_id: int):
    return store.markers(session_id)


@app.delete("/api/markers/{marker_id}")
def del_marker(marker_id: int):
    store.delete_marker(marker_id)
    return {"ok": True}


@app.post("/api/sim/boot")
def sim_boot():
    if capture.sim is None:
        raise HTTPException(400, "Kein Simulator aktiv")
    capture.sim.boot()
    if capture.session_id is not None:
        store.add_marker(capture.session_id, "Boot (Sim)")
    return {"ok": True}


# --- Analyse --------------------------------------------------------------

@app.get("/api/analysis/{session_id}/ids")
def analysis_ids(session_id: int):
    return analysis.id_stats(store.frames(session_id))


@app.get("/api/analysis/{session_id}/windows")
def analysis_windows(session_id: int, before: float = 0.5, after: float = 0.5):
    frames = store.frames(session_id)
    if not frames:
        return []
    return analysis.window_analysis(frames, store.markers(session_id), before, after)


@app.get("/api/analysis/{session_id}/compare")
def analysis_compare(session_id: int, a: int, b: int, can_id: int,
                     before: float = 0.5, after: float = 0.5):
    ms = {m["id"]: m for m in store.markers(session_id)}
    if a not in ms or b not in ms:
        raise HTTPException(404, "Marker nicht gefunden")
    return analysis.compare_markers(store.frames(session_id), ms[a], ms[b], can_id, before, after)


@app.get("/api/analysis/{session_id}/checksum")
def analysis_checksum(session_id: int, can_id: int):
    frames = store.frames(session_id, can_id=can_id)
    return analysis.checksum_test([f["data"] for f in frames])


@app.get("/api/analysis/diff")
def analysis_diff(a: int, b: int, can_id: int | None = None, limit: int = 500):
    fa, fb = store.frames(a), store.frames(b)
    return analysis.sequence_diff(fa, fb, can_id)[:limit]


@app.get("/api/analysis/{session_id}/frames")
def analysis_frames(session_id: int, can_id: int | None = None, t0: float | None = None,
                    t1: float | None = None, limit: int = 2000):
    fr = store.frames(session_id, t0, t1, can_id, limit)
    return [{"ts": f["ts"], "id": f"{f['id']:03X}", "data": f["data"].hex(" "),
             "ascii": analysis.ascii_view(f["data"])} for f in fr]


@app.get("/api/export/{session_id}.csv", response_class=PlainTextResponse)
def export_csv(session_id: int):
    lines = ["ts,id,dlc,data"]
    for f in store.frames(session_id, limit=10_000_000):
        lines.append(f"{f['ts']:.6f},{f['id']:03X},{f['dlc']},{f['data'].hex()}")
    return "\n".join(lines)


# --- Dekoder --------------------------------------------------------------

class DecoderReq(BaseModel):
    can_id: int
    name: str
    layout: list[dict]


@app.get("/api/decoders")
def decoders():
    return {f"{k:03X}": v for k, v in store.decoders().items()}


@app.post("/api/decoders")
def set_decoder(req: DecoderReq):
    store.set_decoder(req.can_id, req.name, req.layout)
    return {"ok": True}


@app.delete("/api/decoders/{can_id}")
def del_decoder(can_id: int):
    store.delete_decoder(can_id)
    return {"ok": True}


# --- WebSocket Live ---------------------------------------------------------

@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    q = capture.subscribe()
    try:
        while True:
            batch = []
            try:
                while len(batch) < 200:
                    batch.append(q.get_nowait())
            except queue.Empty:
                pass
            if batch:
                decs = store.decoders()
                payload = []
                for f in batch:
                    d = f.to_json()
                    if f.id in decs:
                        d["decoded"] = analysis.decode(decs[f.id]["layout"], f.data)
                        d["name"] = decs[f.id]["name"]
                    payload.append(d)
                await ws.send_text(json.dumps(payload))
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass
    finally:
        capture.unsubscribe(q)
