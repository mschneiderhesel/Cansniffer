"""Auswertungen: ID-Statistik, Byte-Klassifikation, Marker-Fenster, Diff, Prüfsummen, Dekoder."""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import groupby


# --- ID-Statistik -------------------------------------------------------

def id_stats(frames: list[dict]) -> list[dict]:
    """Pro CAN-ID: Häufigkeit, DLC, mittlerer Abstand und Byte-Klassifikation."""
    by_id: dict[int, list[dict]] = defaultdict(list)
    for f in frames:
        by_id[f["id"]].append(f)
    out = []
    for cid, fl in sorted(by_id.items()):
        ts = [f["ts"] for f in fl]
        dts = [b - a for a, b in zip(ts, ts[1:])]
        dlc = Counter(f["dlc"] for f in fl).most_common(1)[0][0]
        out.append({
            "id": cid,
            "id_hex": f"{cid:03X}",
            "count": len(fl),
            "dlc": dlc,
            "mean_dt_ms": round(1000 * sum(dts) / len(dts), 1) if dts else None,
            "bytes": classify_bytes([f["data"] for f in fl]),
            "example": fl[-1]["data"].hex(" "),
        })
    return out


def classify_bytes(datas: list[bytes]) -> list[dict]:
    """Je Byte-Position: konstant / Zähler / variabel, plus Wertebereich und Anzahl Werte."""
    maxlen = max((len(d) for d in datas), default=0)
    res = []
    for i in range(maxlen):
        vals = [d[i] for d in datas if len(d) > i]
        distinct = sorted(set(vals))
        if len(distinct) == 1:
            kind = "const"
        else:
            steps = [(b - a) & 0xFF for a, b in zip(vals, vals[1:])]
            inc = sum(1 for s in steps if s == 1)
            kind = "counter" if steps and inc / len(steps) > 0.8 else "var"
        res.append({"pos": i, "kind": kind, "distinct": len(distinct),
                    "min": distinct[0], "max": distinct[-1],
                    "values": distinct[:16]})
    return res


# --- Marker-Fenster -----------------------------------------------------

def window_analysis(frames: list[dict], markers: list[dict], before: float = 0.5,
                    after: float = 0.5) -> list[dict]:
    """Für jeden Marker: welche IDs tauchen im Fenster auf, die außerhalb selten sind."""
    total_time = (frames[-1]["ts"] - frames[0]["ts"]) if len(frames) > 1 else 1.0
    base_rate = Counter(f["id"] for f in frames)
    out = []
    for m in markers:
        t0, t1 = m["ts"] - before, m["ts"] + after
        win = [f for f in frames if t0 <= f["ts"] <= t1]
        cnt = Counter(f["id"] for f in win)
        hits = []
        for cid, n in cnt.items():
            expected = base_rate[cid] / total_time * (before + after)
            score = n / expected if expected > 0 else float("inf")
            if score >= 2.0 or expected < 0.5:
                hits.append({
                    "id": cid, "id_hex": f"{cid:03X}", "count": n,
                    "expected": round(expected, 2), "score": round(score, 1),
                    "frames": [{"dt_ms": round(1000 * (f["ts"] - m["ts"])),
                                "data": f["data"].hex(" ")}
                               for f in win if f["id"] == cid][:12],
                })
        hits.sort(key=lambda h: -h["score"])
        out.append({"marker": m, "window_frames": len(win), "candidates": hits})
    return out


def compare_markers(frames: list[dict], a: dict, b: dict, can_id: int,
                    before: float = 0.5, after: float = 0.5) -> dict:
    """Erster Frame einer ID im Fenster von Marker A vs. Marker B, byteweise verglichen."""
    def first(m):
        for f in frames:
            if f["id"] == can_id and m["ts"] - before <= f["ts"] <= m["ts"] + after:
                return f
        return None
    fa, fb = first(a), first(b)
    if not fa or not fb:
        return {"ok": False, "reason": "Frame in einem der Fenster nicht gefunden"}
    return {"ok": True, "a": fa["data"].hex(" "), "b": fb["data"].hex(" "),
            "diff": byte_diff(fa["data"], fb["data"])}


def byte_diff(x: bytes, y: bytes) -> list[dict]:
    n = max(len(x), len(y))
    return [{"pos": i,
             "a": x[i] if i < len(x) else None,
             "b": y[i] if i < len(y) else None,
             "changed": (x[i] if i < len(x) else None) != (y[i] if i < len(y) else None)}
            for i in range(n)]


# --- Sequenz-Diff (Konfig-Bursts) -------------------------------------

def sequence_diff(fa: list[dict], fb: list[dict], can_id: int | None = None) -> list[dict]:
    """Zwei Aufnahmen Frame für Frame vergleichen (nach Reihenfolge, nicht nach Zeit)."""
    if can_id is not None:
        fa = [f for f in fa if f["id"] == can_id]
        fb = [f for f in fb if f["id"] == can_id]
    out = []
    for i in range(max(len(fa), len(fb))):
        a = fa[i] if i < len(fa) else None
        b = fb[i] if i < len(fb) else None
        if a and b and a["id"] == b["id"] and a["data"] == b["data"]:
            continue
        out.append({
            "index": i,
            "a": {"id": f"{a['id']:03X}", "data": a["data"].hex(" "), "ascii": ascii_view(a["data"])} if a else None,
            "b": {"id": f"{b['id']:03X}", "data": b["data"].hex(" "), "ascii": ascii_view(b["data"])} if b else None,
            "diff": byte_diff(a["data"], b["data"]) if a and b else None,
        })
    return out


def ascii_view(d: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else "." for c in d)


# --- Prüfsummen ---------------------------------------------------------

def _crc8(data: bytes, poly: int, init: int = 0, xorout: int = 0, refl: bool = False) -> int:
    crc = init
    for b in data:
        if refl:
            b = int(f"{b:08b}"[::-1], 2)
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    if refl:
        crc = int(f"{crc:08b}"[::-1], 2)
    return crc ^ xorout


CHECKSUM_CANDIDATES = {
    "xor": lambda d: _xor(d),
    "xor_ff": lambda d: _xor(d) ^ 0xFF,
    "sum": lambda d: sum(d) & 0xFF,
    "sum_neg": lambda d: (-sum(d)) & 0xFF,
    "sum_ff": lambda d: (sum(d) & 0xFF) ^ 0xFF,
    "crc8_07": lambda d: _crc8(d, 0x07),
    "crc8_07_ff": lambda d: _crc8(d, 0x07, 0xFF),
    "crc8_31": lambda d: _crc8(d, 0x31),
    "crc8_31_ff": lambda d: _crc8(d, 0x31, 0xFF),
    "crc8_1d": lambda d: _crc8(d, 0x1D),
    "crc8_1d_ff": lambda d: _crc8(d, 0x1D, 0xFF),
    "crc8_9b": lambda d: _crc8(d, 0x9B),
    "crc8_2f_ff": lambda d: _crc8(d, 0x2F, 0xFF, 0xFF),
}


def _xor(d: bytes) -> int:
    x = 0
    for v in d:
        x ^= v
    return x


def checksum_test(datas: list[bytes]) -> list[dict]:
    """Prüft, ob das letzte oder erste Byte eine Prüfsumme über den Rest ist.

    Testet auch Varianten mit vorangestellter ID-Beteiligung nicht – die wird
    meist nicht einbezogen; wenn nichts passt, ist das ein Hinweis.
    """
    datas = [d for d in datas if len(d) >= 2][:2000]
    if not datas:
        return []
    results = []
    for where in ("last", "first"):
        for name, fn in CHECKSUM_CANDIDATES.items():
            ok = 0
            for d in datas:
                body, cs = (d[:-1], d[-1]) if where == "last" else (d[1:], d[0])
                if fn(body) == cs:
                    ok += 1
            ratio = ok / len(datas)
            if ratio > 0.5:
                results.append({"algo": name, "position": where, "match": round(ratio, 3)})
    results.sort(key=lambda r: -r["match"])
    return results


# --- Dekoder ------------------------------------------------------------

def decode(layout: list[dict], data: bytes) -> dict:
    """layout: [{name, pos, len?, type: u8|u16le|u16be|ascii|bits, scale?, enum?}]"""
    out = {}
    for f in layout:
        pos, ln, typ = f["pos"], f.get("len", 1), f.get("type", "u8")
        if pos >= len(data):
            continue
        chunk = data[pos:pos + ln]
        if typ == "u8":
            v = chunk[0]
        elif typ == "u16le":
            v = int.from_bytes(chunk[:2], "little")
        elif typ == "u16be":
            v = int.from_bytes(chunk[:2], "big")
        elif typ == "ascii":
            v = ascii_view(chunk)
        elif typ == "bits":
            v = f"{chunk[0]:08b}"
        else:
            v = chunk.hex(" ")
        if "scale" in f and isinstance(v, int):
            v = v * f["scale"]
        if "enum" in f and str(v) in f["enum"]:
            v = f["enum"][str(v)]
        out[f["name"]] = v
    return out
