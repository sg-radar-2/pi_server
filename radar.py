"""RETINA-4SN TCP 수신기.

radar-studio/src/device/retina.cpp 의 패킷 해석을 Python으로 옮긴 것이다.
레이더 29172 포트에 TCP로 붙어서 프레임을 받고, situation_core 가 읽는 행(rows) 형식으로 최근 몇 분치를 모아 둔다.
인터페이스는 tools/behavior/live_source.LiveReceiver 와 같다 (start / stop / snapshot / spec).

패킷 (little-endian)
    PacketHeader  uint32 × 9   [1]=magic 0xABCD4321, [4]=packageSize (헤더 뒤 바이트 수)
    FrameHeader   uint64 magic 0x0807060504030201, uint32 frameCount, uint32 pointCount
    Point × N     float x, y, z, doppler, power      (20바이트)
    int32 × N     포인트마다 targetId (-1 = 타깃 아님)
    FrameHeader   (실제 기기: 패킷 시작에서 48056 바이트 위치 / 에뮬레이터: 포인트 바로 뒤) targetCount
    Target × M    float x, y, uint32 status, uint32 targetId, float × 3   (28바이트)

행의 target_id 는 레이더 ID 그대로가 아니라 "등장 번호(uid)"다. 같은 레이더 ID라도 UID_GAP_S 넘게 안 보였다가
다시 나오면 새 번호를 준다. 레이더가 ID를 재사용해도 다른 사람이 한 트랙으로 섞이지 않고, 서버가 사람을
구분하는 키(가장 이른 uid)가 몇 분 동안 바뀌지 않게 하려는 것이다.
"""
from __future__ import annotations

import concurrent.futures as cf
import ipaddress
import logging
import socket
import struct
import threading
import time
from collections import deque

log = logging.getLogger("radar")

DEVICE_PORT = 29172
PACKET_MAGIC = struct.pack("<I", 0xABCD4321)
FRAME_MAGIC = 0x0807060504030201
TARGET_OFFSET = 48056
MAX_PACKET = 16 * 1024 * 1024

PACKET_HEADER = struct.Struct("<9I")   # 36
FRAME_HEADER = struct.Struct("<QII")   # 16
POINT = struct.Struct("<5f")           # 20
TARGET = struct.Struct("<ffII3f")      # 28

SESSION = "실시간"
UID_GAP_S = 5.0       # 같은 레이더 ID가 이보다 오래 안 보였다 나오면 새 등장 번호
STALE_S = 2.0         # 이보다 오래 프레임이 없으면 '수신 끊김'


def parse_packet(buf: bytes) -> tuple[int, list[tuple], list[tuple]] | None:
    """패킷 하나 → (frameCount, points[(x,y,z,doppler,power,tid)], targets[(x,y,status,tid)])"""
    if len(buf) < PACKET_HEADER.size + FRAME_HEADER.size:
        return None
    off = PACKET_HEADER.size
    magic, frame_count, n_points = FRAME_HEADER.unpack_from(buf, off)
    if magic != FRAME_MAGIC:
        return None
    off += FRAME_HEADER.size
    need = off + n_points * (POINT.size + 4)
    if need > len(buf):
        return None
    pts = [POINT.unpack_from(buf, off + i * POINT.size) for i in range(n_points)]
    off += n_points * POINT.size
    tids = struct.unpack_from(f"<{n_points}i", buf, off) if n_points else ()
    off += n_points * 4
    points = [p + (t,) for p, t in zip(pts, tids)]
    targets: list[tuple] = []
    if off >= len(buf):
        return frame_count, points, targets
    # 에뮬레이터는 포인트 바로 뒤에, 실제 기기는 고정 위치(TARGET_OFFSET)에 타깃 구역을 둔다
    right_after = off + FRAME_HEADER.size <= len(buf) and FRAME_HEADER.unpack_from(buf, off)[0] == FRAME_MAGIC
    if not right_after:
        off = TARGET_OFFSET
    if off + FRAME_HEADER.size > len(buf):
        return frame_count, points, targets
    magic, _fc, n_targets = FRAME_HEADER.unpack_from(buf, off)
    if magic != FRAME_MAGIC:
        return None
    off += FRAME_HEADER.size
    for i in range(n_targets):
        if off + TARGET.size > len(buf):
            break
        x, y, status, tid, *_ = TARGET.unpack_from(buf, off)
        targets.append((x, y, status, tid))
        off += TARGET.size
    return frame_count, points, targets


class PacketStream:
    """TCP 바이트 흐름에서 패킷을 잘라낸다 (DeviceClient::tryExtractPacket 과 같은 방식)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(PACKET_MAGIC)
            if i < 0:
                del self.buf[:-7]            # 경계에 걸친 magic 을 위해 끝 몇 바이트만 남김
                return out
            if i < 4:                        # magic 앞의 reserved0 4바이트가 없음 = 앞이 잘린 조각: 버림
                del self.buf[:i + 4]
                continue
            if i > 4:
                del self.buf[:i - 4]
            if len(self.buf) < PACKET_HEADER.size:
                return out
            size = PACKET_HEADER.unpack_from(self.buf, 0)[4]
            if size > MAX_PACKET:
                del self.buf[:8]             # 말이 안 되는 크기: 이 magic 은 버림
                continue
            total = PACKET_HEADER.size + size
            if len(self.buf) < total:
                return out
            out.append(bytes(self.buf[:total]))
            del self.buf[:total]


def target_rows(t: float, frame_id: int, points: list[tuple], targets: list[tuple], uid_of) -> list[dict]:
    """프레임 하나 → 타깃마다 행 하나 (live_relay.cpp 와 같은 도플러 통계)"""
    by_tid: dict[int, list[float]] = {}
    for p in points:
        if p[5] >= 0:
            by_tid.setdefault(int(p[5]), []).append(p[3])
    rows = []
    for x, y, status, tid in targets:
        d = by_tid.get(int(tid), [])
        n = len(d)
        if n:
            mean = sum(d) / n
            abs_mean = sum(abs(v) for v in d) / n
            var = sum(v * v for v in d) / n - mean * mean
            std = max(var, 0.0) ** 0.5
        else:
            mean = abs_mean = std = float("nan")
        rows.append({"session": SESSION, "time_s": t, "frame_id": frame_id, "target_id": uid_of(int(tid), t),
                     "radar_id": int(tid), "x": float(x), "y": float(y), "status": int(status), "n_points": n,
                     "doppler_mean": mean, "doppler_abs_mean": abs_mean, "doppler_std": std})
    return rows


def local_ipv4() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def find_radar(port: int = DEVICE_PORT, timeout: float = 0.3) -> str | None:
    """같은 /24 대역에서 29172 포트가 열린 기기를 찾는다 (DeviceFinder 와 같은 생각, 단순화)."""
    me = local_ipv4()
    if not me:
        return None
    net = ipaddress.ip_network(f"{me}/24", strict=False)

    def probe(ip: str) -> str | None:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return ip
        except OSError:
            return None

    hosts = [str(h) for h in net.hosts() if str(h) != me]
    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        for ip in ex.map(probe, hosts):
            if ip:
                return ip
    return None


class RadarReceiver:
    """레이더에 TCP로 붙어서 최근 window_s 초의 행을 모아 둔다 (뒤 스레드, 끊기면 다시 붙음)."""

    def __init__(self, host: str = "", port: int = DEVICE_PORT, window_s: float = 180.0):
        self.host = host                  # "" 이면 같은 대역에서 찾음
        self.port = port
        self.window_s = window_s
        self._rows: deque = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._t0 = time.monotonic()
        self._uids: dict[int, tuple[int, float]] = {}   # 레이더 ID → (등장 번호, 마지막으로 본 시각)
        self._next_uid = 1
        self._rx_times: deque = deque(maxlen=200)
        self.version = 0
        self.frames = 0
        self.last_rx = 0.0               # time.monotonic()
        self.connected_host: str | None = None
        self.state = "connecting"        # connecting | ok | disconnected
        self.error: str | None = None
        self.latest: dict[int, dict] = {}   # 마지막 프레임의 타깃들 (uid → 행)
        self.spec: dict | None = None    # 센서 설정은 서버 설정 파일에서 넣는다

    # ---- 시간
    def now(self) -> float:
        """수신기 시각 (행의 time_s 와 같은 기준, 초)"""
        return time.monotonic() - self._t0

    def mono_of(self, t: float) -> float:
        return self._t0 + t

    # ---- 제어
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="radar", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def reconnect(self) -> None:
        """지금 연결을 끊고 바로 다시 붙는다 (앱의 '레이더 다시 연결')."""
        self._close()
        self._kick.set()

    def snapshot(self) -> tuple[list[dict], int]:
        with self._lock:
            return list(self._rows), self.version

    def frame_rate(self) -> float:
        now = time.monotonic()
        ts = [t for t in self._rx_times if now - t <= 5.0]
        return round((len(ts) - 1) / (ts[-1] - ts[0]), 1) if len(ts) >= 2 and ts[-1] > ts[0] else 0.0

    def radar_state(self) -> str:
        if self.state == "ok" and time.monotonic() - self.last_rx > STALE_S:
            return "stale"
        return self.state

    # ---- 내부
    def _close(self) -> None:
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def _uid_of(self, radar_id: int, t: float) -> int:
        uid, last = self._uids.get(radar_id, (0, -1e9))
        if not uid or t - last > UID_GAP_S:
            uid = self._next_uid
            self._next_uid += 1
        self._uids[radar_id] = (uid, t)
        return uid

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            host = self.host
            if not host:
                self.state = "connecting"
                log.info("레이더 찾는 중 (같은 대역에서 %d 포트)", self.port)
                host = find_radar(self.port) or ""
                if not host:
                    self.error = "같은 Wi-Fi에서 레이더를 못 찾음"
                    self._wait(5.0)
                    continue
                log.info("레이더 찾음: %s", host)
            try:
                self.state = "connecting"
                s = socket.create_connection((host, self.port), timeout=5.0)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                s.settimeout(3.0)
                self._sock = s
                self.connected_host, self.state, self.error = host, "ok", None
                log.info("레이더 연결됨: %s:%d", host, self.port)
                delay = 1.0
                self._read(s)
            except OSError as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                log.warning("레이더 연결 실패/끊김: %s", self.error)
            finally:
                self._close()
                if not self._stop.is_set():
                    self.state = "disconnected"
            self._wait(delay)
            delay = min(delay * 2, 8.0)

    def _wait(self, s: float) -> None:
        self._kick.wait(s)
        self._kick.clear()

    def _read(self, s: socket.socket) -> None:
        stream = PacketStream()
        while not self._stop.is_set():
            try:
                data = s.recv(65536)
            except socket.timeout:
                if time.monotonic() - self.last_rx > 10.0:
                    raise OSError("10초 동안 데이터 없음")
                continue
            if not data:
                raise OSError("레이더가 연결을 닫음")
            for pkt in stream.feed(data):
                parsed = parse_packet(pkt)
                if parsed is None:
                    continue
                self._add(*parsed)

    def _add(self, frame_id: int, points: list[tuple], targets: list[tuple]) -> None:
        t = self.now()
        rows = target_rows(t, frame_id, points, targets, self._uid_of)
        with self._lock:
            self._rows.extend(rows)
            while self._rows and self._rows[0]["time_s"] < t - self.window_s:
                self._rows.popleft()
            self.latest = {r["target_id"]: r for r in rows}
            self.frames += 1
            self.version += 1
            self.last_rx = time.monotonic()
            self._rx_times.append(self.last_rx)
            self.state = "ok"
        if len(self._uids) > 512:            # 오래 안 보인 레이더 ID 정리
            old = [k for k, (_, last) in self._uids.items() if t - last > 600]
            for k in old:
                del self._uids[k]
