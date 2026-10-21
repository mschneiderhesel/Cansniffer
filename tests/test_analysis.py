import time

from cansniffer.analysis import checksum_test, classify_bytes, decode, id_stats, sequence_diff, window_analysis
from cansniffer.capture import Simulator


def sim_frames(n_ticks=60, press_at=30):
    sim = Simulator(heads=3)
    frames = []
    t = time.time()
    for i in range(n_ticks):
        for h in sim.next_hb:
            sim.next_hb[h] = 0  # Heartbeats erzwingen
        if i == press_at:
            sim.press(2, 5)
        for f in sim.tick():
            frames.append({"ts": t, "id": f.id, "ext": False, "dlc": len(f.data), "data": f.data})
        t += 0.2
    return frames, t


def test_id_stats_and_bytes():
    frames, _ = sim_frames()
    st = {s["id"]: s for s in id_stats(frames)}
    hb = st[0x101]
    assert hb["bytes"][0]["kind"] == "const"
    assert hb["bytes"][2]["kind"] == "counter"


def test_checksum_xor_detected():
    frames, _ = sim_frames()
    res = checksum_test([f["data"] for f in frames if f["id"] == 0x101])
    assert res and res[0]["algo"] == "xor" and res[0]["position"] == "last"


def test_window_finds_keypress():
    frames, _ = sim_frames()
    press_ts = next(f["ts"] for f in frames if f["id"] == 0x202)
    res = window_analysis(frames, [{"id": 1, "ts": press_ts, "label": "Taste 5", "head": 2, "key": 5}])
    ids = [c["id"] for c in res[0]["candidates"]]
    assert 0x202 in ids


def test_sequence_diff_and_decode():
    a = [{"ts": 0, "id": 0x700, "dlc": 8, "data": b"\x01\x00PILS 0"}]
    b = [{"ts": 0, "id": 0x700, "dlc": 8, "data": b"\x01\x00BIER 0"}]
    d = sequence_diff(a, b)
    assert d and d[0]["a"]["ascii"].endswith("PILS 0")
    out = decode([{"name": "kopf", "pos": 0}, {"name": "text", "pos": 2, "len": 6, "type": "ascii"}], a[0]["data"])
    assert out == {"kopf": 1, "text": "PILS 0"}
