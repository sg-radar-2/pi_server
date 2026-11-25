"""가짜 RETINA-4SN: 29172 포트를 열고 대본대로 움직이는 사람을 레이더 패킷으로 보낸다.

레이더·파이 없이 server.py 를 시험할 때 쓴다 (실제 녹화를 재생하려면 radar-emulator 를 쓴다).
    python fake_radar.py                 # 127.0.0.1:29172, 초당 10프레임
    python fake_radar.py --layout real   # 타깃 구역을 실제 기기처럼 48056 위치에
    python server.py --radar 127.0.0.1 --camera fake

대본: 방문(문 앞에서 기다렸다 들어감) → 장시간 체류(25초 머물다 외곽으로) → 통과 → 반복

실제 녹화 재생 (트랙 CSV 의 타깃 위치·포인트 수·도플러를 그대로 레이더 패킷으로 만들어 녹화 속도로 보냄):
    python fake_radar.py --replay recordings/6-3_tracks_raw.csv recordings/7-4_tracks_raw.csv
    python fake_radar.py --replay recordings/            # 폴더의 *_tracks_raw.csv 전부, 녹화 사이 40초 쉼
    (녹화 사이 쉬는 시간이 30초보다 짧으면 서버가 앞 녹화의 사람과 다음 녹화의 사람을 같은 사람으로 이어 붙인다)
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import random
import socket
import struct
import time
from pathlib import Path

FRAME_MAGIC = 0x0807060504030201
PACKET_MAGIC = 0xABCD4321
TARGET_OFFSET = 48056
FPS = 10


def lerp(a, b, n):
    return [(a[0] + (b[0] - a[0]) * i / (n - 1), a[1] + (b[1] - a[1]) * i / (n - 1)) for i in range(n)]


def scenes():
    n = FPS
    visit = lerp((1.7, 2.8), (0.3, 0.8), 5 * n) + [(0.3, 0.8)] * (5 * n) + lerp((0.3, 0.8), (0.05, 0.25), 1 * n)
    stay = [(0.9 + 0.04 * math.sin(i / 6), 1.2 + 0.03 * math.cos(i / 8)) for i in range(25 * n)]
    dwell = lerp((-1.8, 2.7), (0.9, 1.2), 5 * n) + stay + lerp((0.9, 1.2), (1.95, 2.6), 4 * n)
    cross = lerp((-1.95, 2.0), (1.95, 2.2), 6 * n)
    return [("방문", visit), ("장시간 체류", dwell), ("통과", cross)]


def packet(frame_count: int, targets: list[tuple[int, float, float, float, float]], layout: str) -> bytes:
    """targets: (tid, x, y, vx, vy)"""
    pts, ids = [], []
    for tid, x, y, vx, vy in targets:
        r = math.hypot(x, y) or 1.0
        vr = (x * vx + y * vy) / r
        for _ in range(40):
            pts.append(struct.pack("<5f", x + random.gauss(0, 0.12), y + random.gauss(0, 0.08),
                                   random.uniform(-0.6, 0.9), vr + random.gauss(0, 0.05), random.uniform(10, 30)))
            ids.append(tid)
    for _ in range(15):                            # 잡음 포인트
        pts.append(struct.pack("<5f", random.uniform(-2, 2), random.uniform(0, 3), random.uniform(-1, 2), 0.0, 5.0))
        ids.append(-1)
    body = struct.pack("<QII", FRAME_MAGIC, frame_count, len(pts)) + b"".join(pts) + struct.pack(f"<{len(ids)}i", *ids)
    tsec = struct.pack("<QII", FRAME_MAGIC, frame_count, len(targets)) + b"".join(
        struct.pack("<ffII3f", x, y, 4 if math.hypot(vx, vy) > 0.2 else 0, tid, 0, 0, 0)
        for tid, x, y, vx, vy in targets)
    if layout == "real":
        pad = TARGET_OFFSET - 36 - len(body)
        body += b"\0" * max(pad, 0)
    body += tsec
    head = struct.pack("<9I", 0, PACKET_MAGIC, 0, 0, len(body), 0, 0, 0, 0)
    return head + body


def read_rows(path: Path) -> list[dict]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp949")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def num(v, default=float("nan")) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def recording_frames(path: Path) -> list[tuple[float, list[tuple]]]:
    """트랙 CSV → [(time_s, [(tid, x, y, status, n_points, doppler_mean, doppler_std), ...]), ...]"""
    frames: dict[tuple, list] = {}
    for r in read_rows(path):
        key = (r.get("session") or "", int(num(r.get("frame_id"), 0)), num(r.get("time_s"), 0.0))
        frames.setdefault(key, []).append((int(num(r["target_id"], 0)), num(r["x"]), num(r["y"]),
                                           int(num(r.get("status"), 0)), int(num(r.get("n_points"), 0)),
                                           num(r.get("doppler_mean"), 0.0), num(r.get("doppler_std"), 0.0)))
    return [(k[2], v) for k, v in sorted(frames.items(), key=lambda kv: (kv[0][0], kv[0][2]))]


def replay_packet(frame_count: int, targets: list[tuple]) -> bytes:
    """녹화의 한 프레임 → 레이더 패킷. 포인트 수와 도플러 평균·표준편차가 녹화와 같게 포인트를 만든다."""
    pts, ids = [], []
    for tid, x, y, _status, n, dmean, dstd in targets:
        dmean = 0.0 if math.isnan(dmean) else dmean
        dstd = 0.0 if math.isnan(dstd) else dstd
        for i in range(n):
            d = dmean if (n % 2 and i == n - 1) else dmean + (dstd if i % 2 == 0 else -dstd)
            pts.append(struct.pack("<5f", x + random.gauss(0, 0.1), y + random.gauss(0, 0.07),
                                   random.uniform(-0.6, 0.9), d, random.uniform(10, 30)))
            ids.append(tid)
    body = struct.pack("<QII", FRAME_MAGIC, frame_count, len(pts)) + b"".join(pts) + struct.pack(f"<{len(ids)}i", *ids)
    body += struct.pack("<QII", FRAME_MAGIC, frame_count, len(targets)) + b"".join(
        struct.pack("<ffII3f", x, y, status, tid, 0, 0, 0) for tid, x, y, status, *_ in targets)
    head = struct.pack("<9I", 0, PACKET_MAGIC, 0, 0, len(body), 0, 0, 0, 0)
    return head + body


def run_replay(conn: socket.socket, files: list[Path], gap_s: float) -> None:
    fc = 0
    while True:
        for path in files:
            frames = recording_frames(path)
            if not frames:
                continue
            print(f"  재생: {path.name} ({frames[-1][0] - frames[0][0]:.0f}초, {len(frames)}프레임)")
            prev_t = frames[0][0]
            for t, targets in frames:
                time.sleep(min(max(t - prev_t, 0.0), 1.0))
                prev_t = t
                fc += 1
                conn.sendall(replay_packet(fc, targets))
            for _ in range(int(gap_s * FPS)):      # 녹화 사이 아무도 없는 시간 (사람이 사라져야 최종 판정이 나옴)
                fc += 1
                conn.sendall(replay_packet(fc, []))
                time.sleep(1 / FPS)


def run(conn: socket.socket, layout: str) -> None:
    fc, tid = 0, 0
    while True:
        for name, path in scenes():
            tid = tid % 30 + 1
            print(f"  {name} (레이더 ID {tid})")
            prev = path[0]
            for x, y in path:
                vx, vy = (x - prev[0]) * FPS, (y - prev[1]) * FPS
                prev = (x, y)
                fc += 1
                conn.sendall(packet(fc, [(tid, x, y, vx, vy)], layout))
                time.sleep(1 / FPS)
            for _ in range(3 * FPS):               # 아무도 없는 3초
                fc += 1
                conn.sendall(packet(fc, [], layout))
                time.sleep(1 / FPS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=29172)
    ap.add_argument("--layout", choices=["emulator", "real"], default="emulator")
    ap.add_argument("--replay", nargs="+", type=Path, help="재생할 트랙 CSV 파일 또는 폴더")
    ap.add_argument("--gap", type=float, default=40.0, help="녹화 사이 쉬는 시간(초)")
    a = ap.parse_args()
    files: list[Path] = []
    for p in a.replay or []:
        files += sorted(p.glob("*_tracks_raw.csv")) if p.is_dir() else [p]
    if a.replay and not files:
        raise SystemExit("재생할 트랙 CSV가 없습니다")
    srv = socket.create_server((a.host, a.port), reuse_port=False)
    print(f"가짜 레이더 {a.host}:{a.port} ({a.layout}) - 서버가 붙기를 기다리는 중")
    while True:
        conn, addr = srv.accept()
        print(f"연결됨: {addr}")
        try:
            run_replay(conn, files, a.gap) if files else run(conn, a.layout)
        except OSError as exc:
            print(f"끊김: {exc}")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
