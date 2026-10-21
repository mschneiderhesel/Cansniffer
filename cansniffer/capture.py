"""Capture-Thread: liest Frames von python-can oder vom Simulator und schreibt sie in den Store."""
from __future__ import annotations

import collections
import queue
import random
import threading
import time
from dataclasses import dataclass

from .db import Store


@dataclass
class Frame:
    ts: float
    id: int
    ext: bool
    data: bytes

    def to_json(self) -> dict:
        return {"ts": self.ts, "id": self.id, "ext": self.ext, "dlc": len(self.data),
                "data": self.data.hex(" ")}


class Capture:
    """Hält einen Hintergrund-Thread, der Frames liest und an Store + Abonnenten verteilt."""

    def __init__(self, store: Store):
        self.store = store
        self.session_id: int | None = None
        self.recording = False
        self.source_desc = "none"
        self._bus = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.recent: collections.deque[Frame] = collections.deque(maxlen=5000)
        self.stats: dict[int, dict] = {}
        self.subscribers: list[queue.Queue] = []
        self.sim: Simulator | None = None
        self.frame_count = 0
        self.error: str | None = None

    # --- Quelle --------------------------------------------------------
    def open(self, interface: str, channel: str, bitrate: int, listen_only: bool = True) -> None:
        self.close()
        self._stop.clear()
        if interface == "sim":
            self.sim = Simulator()
            self.source_desc = "Simulator"
            self._thread = threading.Thread(target=self._run_sim, daemon=True)
        else:
            import can  # lazy, damit die App auch ohne python-can importierbar bleibt
            kwargs = {"interface": interface, "channel": channel, "bitrate": bitrate}
            if interface in ("slcan", "socketcan", "pcan", "gs_usb"):
                kwargs["receive_own_messages"] = False
            if listen_only and interface in ("slcan", "socketcan"):
                kwargs["listen_only"] = True
            self._bus = can.Bus(**kwargs)
            self.source_desc = f"{interface}:{channel}@{bitrate}" + (" (listen-only)" if listen_only else "")
            self._thread = threading.Thread(target=self._run_bus, daemon=True)
        self.error = None
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:
                pass
        self._bus = None
        self.sim = None
        self.source_desc = "none"

    def is_open(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # --- Aufnahme ------------------------------------------------------
    def start_recording(self, name: str, note: str = "") -> int:
        self.session_id = self.store.new_session(name, note)
        self.recording = True
        return self.session_id

    def stop_recording(self) -> None:
        self.recording = False
        self.store.flush()

    # --- Verteilung ----------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    def _dispatch(self, f: Frame) -> None:
        self.frame_count += 1
        self.recent.append(f)
        st = self.stats.setdefault(f.id, {"count": 0, "dlc": len(f.data), "last": None,
                                          "last_ts": 0.0, "min_dt": None})
        if st["last_ts"]:
            dt = f.ts - st["last_ts"]
            st["min_dt"] = dt if st["min_dt"] is None else min(st["min_dt"], dt)
        st["count"] += 1
        st["dlc"] = len(f.data)
        st["last"] = f.data.hex(" ")
        st["last_ts"] = f.ts
        if self.recording and self.session_id is not None:
            self.store.add_frame(self.session_id, f.ts, f.id, f.ext, f.data)
        for q in list(self.subscribers):
            try:
                q.put_nowait(f)
            except queue.Full:
                pass

    def _run_bus(self) -> None:
        try:
            while not self._stop.is_set():
                msg = self._bus.recv(timeout=0.2)
                if msg is None:
                    continue
                self._dispatch(Frame(msg.timestamp or time.time(), msg.arbitration_id,
                                     msg.is_extended_id, bytes(msg.data)))
        except Exception as e:  # pragma: no cover
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.store.flush()

    def _run_sim(self) -> None:
        assert self.sim is not None
        while not self._stop.is_set():
            for f in self.sim.tick():
                self._dispatch(f)
            time.sleep(0.01)
        self.store.flush()


class Simulator:
    """Fiktiver NGT-Bus mit 10 Köpfen – nur zum Testen der Oberfläche.

    Erfundenes Protokoll:
      0x100+head : Heartbeat alle 200 ms   [head, status, counter, 0, 0, 0, 0, xor]
      0x200+head : Tastendruck             [head, page, key, 0x01, 0, 0, 0, xor]
      0x300+head : Zapfstatus              [head, ml_lo, ml_hi, state, 0, 0, 0, xor]
      0x700      : Konfig-Burst beim Boot  [head, seq, ascii x6]
    """

    def __init__(self, heads: int = 10):
        self.heads = heads
        self.counter = [0] * (heads + 1)
        self.next_hb = {h: time.time() + random.random() * 0.2 for h in range(1, heads + 1)}
        self.pending: list[Frame] = []
        self.pour: dict[int, dict] = {}

    @staticmethod
    def _xor(b: bytes) -> int:
        x = 0
        for v in b:
            x ^= v
        return x

    def _frame(self, can_id: int, payload: list[int]) -> Frame:
        body = bytes(payload[:7])
        return Frame(time.time(), can_id, False, body + bytes([self._xor(body)]))

    def tick(self) -> list[Frame]:
        out: list[Frame] = []
        now = time.time()
        for h in range(1, self.heads + 1):
            if now >= self.next_hb[h]:
                self.counter[h] = (self.counter[h] + 1) & 0xFF
                status = 0x02 if h in self.pour else 0x01
                out.append(self._frame(0x100 + h, [h, status, self.counter[h], 0, 0, 0, 0]))
                self.next_hb[h] = now + 0.2
        for h, p in list(self.pour.items()):
            if now - p["last"] >= 0.1:
                p["ml"] = min(p["target"], p["ml"] + 10)
                p["last"] = now
                state = 2 if p["ml"] < p["target"] else 3
                out.append(self._frame(0x300 + h, [h, p["ml"] & 0xFF, p["ml"] >> 8, state, 0, 0, 0]))
                if state == 3:
                    del self.pour[h]
        out.extend(self.pending)
        self.pending.clear()
        return out

    def press(self, head: int, key: int, page: int = 1) -> None:
        self.pending.append(self._frame(0x200 + head, [head, page, key, 1, 0, 0, 0]))
        self.pour[head] = {"ml": 0, "target": 100 * key, "last": time.time()}

    def boot(self) -> None:
        for h in range(1, self.heads + 1):
            labels = [f"PILS 0,{k}L".ljust(12)[:12] for k in (3, 5)]
            text = "".join(labels).encode("ascii")
            for seq, i in enumerate(range(0, len(text), 6)):
                chunk = list(text[i:i + 6])
                self.pending.append(Frame(time.time(), 0x700, False, bytes([h, seq] + chunk)))
