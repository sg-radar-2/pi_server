"""도어 레이더 라즈베리파이 서버.

레이더(RETINA-4SN, TCP 29172) → 실시간 판정(situation_core) → 카메라 촬영 → 앱(REST + WebSocket).
API 와 JSON 형식은 door_radar_app/docs/SPEC.md 그대로다. 앱의 mock_server/server.py 와 같은 경로를 연다.

실행
    python server.py                       # config.toml 을 읽음 (없으면 기본값)
    python server.py --config my.toml
    python server.py --radar 127.0.0.1     # PC 에서 radar-emulator 나 fake_radar.py 에 붙여 시험
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import socket
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DEFAULT_BEHAVIOR = HERE.parent / "behavior"       # 저장소 안에서 돌릴 때 (PC)


# ------------------------------------------------------------------ 설정

DEFAULTS: dict[str, Any] = {
    "device_name": "doorradar",
    "http_port": 8080,
    "timezone": "Asia/Seoul",
    "data_dir": "~/doorradar-data",
    "pair_code": "",                 # 비우면 켤 때마다 새 6자리
    "behavior_dir": "",              # situation_core.py 가 있는 폴더 (비우면 ./behavior 또는 ../behavior)
    "model": "models/situation_trees_real.pkl",   # behavior_dir 기준. 지금 코드(모델 버전)와 맞아야 함
    "device_keep_days": 30,
    "radar": {"host": "", "port": 29172, "window_s": 180.0},
    "judge": {"interval_s": 0.5, "final_delay_s": 3.0},
    "camera": {"backend": "auto", "width": 1920, "height": 1080, "helper_python": "/usr/bin/python3"},
    "mdns": {"enabled": True},
    "cloud": {"url": "", "device_key": "", "device_id": "rad2pi", "timeout_s": 30.0,
              "live_enabled": True, "live_fps": 5.0, "live_status_interval_s": 5.0},
}


def load_config(path: Path | None) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if path and path.exists():
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


ap = argparse.ArgumentParser(description="도어 레이더 라즈베리파이 서버")
ap.add_argument("--config", type=Path, default=HERE / "config.toml")
ap.add_argument("--radar", help="레이더 주소 (설정 파일보다 우선)")
ap.add_argument("--camera", help="카메라 backend: auto | helper | rpicam | fake | none")
ap.add_argument("--port", type=int, help="HTTP 포트")
ap.add_argument("--log", default=os.environ.get("DOORRADAR_LOG", "INFO"))
ARGS, _ = ap.parse_known_args()
CFG = load_config(ARGS.config)
if ARGS.radar is not None:
    CFG["radar"]["host"] = ARGS.radar
if ARGS.camera:
    CFG["camera"]["backend"] = ARGS.camera
if ARGS.port:
    CFG["http_port"] = ARGS.port
if os.environ.get("DOORRADAR_DEVICE_KEY"):                  # 기기 키를 config.toml 에 안 적고 싶을 때
    CFG["cloud"]["device_key"] = os.environ["DOORRADAR_DEVICE_KEY"]

logging.basicConfig(level=ARGS.log.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

def find_behavior() -> Path:
    """situation_core.py 가 있는 폴더: 설정값 → ./behavior(파이) → ../behavior(저장소 안) → ../sg-radar-tools/tools/behavior(Capstone)"""
    if CFG["behavior_dir"]:
        return Path(CFG["behavior_dir"]).expanduser()
    for cand in (HERE / "behavior", DEFAULT_BEHAVIOR, HERE.parent / "sg-radar-tools" / "tools" / "behavior"):
        if (cand / "situation_core.py").exists():
            return cand
    raise SystemExit("situation_core.py 를 못 찾음: config.toml 의 behavior_dir 에 tools/behavior 폴더를 적어 주세요")


behavior_dir = find_behavior()
sys.path.insert(0, str(behavior_dir))

import situation_core as C  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

from camera import Camera, thumb_path  # noqa: E402
from cloud import CloudClient  # noqa: E402
from cloud_live import CloudLiveClient  # noqa: E402
from judge import LiveJudge  # noqa: E402
from radar import RadarReceiver  # noqa: E402
from store import Store  # noqa: E402

TZ = ZoneInfo(CFG["timezone"])
DATA = Path(CFG["data_dir"]).expanduser()
FPS = 5
BOOT = str(int(time.time()))           # 사람 키 앞에 붙여서 서버를 다시 켜도 겹치지 않게
SITUATIONS = list(C.SITUATIONS) if hasattr(C, "SITUATIONS") else []
ABNORMAL = set(C.ABNORMAL)
VERSION = "0.1.0"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


# ------------------------------------------------------------------ 구성 요소

def load_model() -> tuple[Path, Any]:
    """설정의 모델을 읽는다. 지금 코드와 안 맞으면(예전 버전) models/ 에서 맞는 것 중 가장 최근 것을 쓴다."""
    want = Path(CFG["model"])
    want = want if want.is_absolute() else behavior_dir / want
    try:
        return want, C.load_models(want)
    except (OSError, ValueError) as exc:
        log.error("모델 %s 를 못 씀: %s", want.name, exc)
    for p in sorted(want.parent.glob("*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            m = C.load_models(p)
        except Exception:  # noqa: BLE001
            continue
        log.warning("대신 %s 를 씀 (config.toml 의 model 을 고쳐 주세요)", p.name)
        return p, m
    raise SystemExit(f"{want.parent} 에 지금 코드(모델 버전 {C.MODEL_VERSION})와 맞는 모델이 없습니다. "
                     "train_situation_gui.pyw 로 다시 학습하세요.")


model_path, MODELS = load_model()
REGION = C.model_region(MODELS)
store = Store(DATA, TZ)
rx = RadarReceiver(CFG["radar"]["host"], int(CFG["radar"]["port"]), float(CFG["radar"]["window_s"]))
cam = Camera(CFG["camera"]["backend"], int(CFG["camera"]["width"]), int(CFG["camera"]["height"]),
             CFG["camera"]["helper_python"])
cloud = CloudClient(CFG["cloud"], DATA, store.get, store.photo_file, thumb_path)
pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="work")
PAIR_CODE = str(CFG["pair_code"] or f"{random.randint(0, 999999):06d}")
STARTED = time.monotonic()
CLIENTS: set[WebSocket] = set()
LOOP: asyncio.AbstractEventLoop | None = None
OUTBOX: asyncio.Queue | None = None


def wall(t: float) -> datetime:
    """수신기 시각(초) → 실제 시각"""
    return datetime.now(TZ) - timedelta(seconds=rx.now() - t)


def iso(t: float) -> str:
    return wall(t).isoformat(timespec="milliseconds")


def emit(msg: dict) -> None:
    """판정 스레드 등에서 WebSocket 으로 보낼 메시지를 넣는다."""
    if LOOP is not None and OUTBOX is not None:
        LOOP.call_soon_threadsafe(OUTBOX.put_nowait, msg)


def summary(ev: dict) -> dict:
    return {k: v for k, v in ev.items() if k not in ("track", "behaviors")}


# ------------------------------------------------------------------ 판정 → 이벤트

def on_camera(info: dict, shot: dict) -> None:
    pool.submit(_camera_event, info, shot)


def _camera_event(info: dict, shot: dict) -> None:
    key = f"{BOOT}:{info['key']}"
    event_id = store.reserve_id()
    t = wall(shot["t"])
    photo = None
    if store.settings.get("camera_on", True) and cam.backend != "none":
        p = store.photo_path(event_id, t)
        if cam.capture(p):
            photo = p
    ev = {"kind": "camera", "person": info["name"], "t": t.isoformat(timespec="milliseconds"),
          "started_at": iso(info["t0"]), "ended_at": None, "situation": shot["situation"],
          "prob": round(float(shot["prob"]), 2), "abnormal": shot["situation"] in ABNORMAL, "label": None}
    saved = store.put_camera(event_id, key, ev, photo)
    log.info("촬영 이벤트 %d: %s %s (%s)", event_id, info["name"], shot["situation"], "사진" if photo else "사진 없음")
    emit({"type": "event", "event": summary(saved)})
    cloud.enqueue(event_id)


def on_final(info: dict) -> None:
    pool.submit(_final_event, info)


def _final_event(info: dict) -> None:
    f = info["final"]
    if f["situation"] in ("", C.UNDECIDED):
        return
    key = f"{BOOT}:{info['key']}"
    ev = {"kind": "final", "person": info["name"], "t": iso(info["t1"]),
          "started_at": iso(info["t0"]), "ended_at": iso(info["t1"]),
          "situation": f["situation"], "prob": f["prob"], "abnormal": f["situation"] in ABNORMAL,
          "also": f["also"], "end_situation": f["end_situation"], "behaviors": f["behaviors"],
          "reason": f["reason"], "path": f["path"], "label": None, "track": info["track"]}
    rows, _ = rx.snapshot()
    uids = set(info["uids"])
    mine = [r for r in rows if r["target_id"] in uids]
    saved, created = store.upsert_final(key, ev, mine)
    log.info("최종 판정 %d%s: %s %s %.0f%%", saved["id"], "" if created else " (갱신)", info["name"],
             f["situation"], f["prob"] * 100)
    emit({"type": "event", "event": summary(saved)})
    cloud.enqueue(saved["id"])


judge = LiveJudge(rx, MODELS, lambda: store.settings, on_camera, on_final,
                  float(CFG["judge"]["interval_s"]), float(CFG["judge"]["final_delay_s"]))


# ------------------------------------------------------------------ 실시간 프레임

def build_frame() -> dict:
    now_mono = time.monotonic()
    latest = rx.latest if now_mono - rx.last_rx < 0.6 else {}
    people, used = [], set()
    for p in judge.snapshot():
        x, y, hidden = p["x"], p["y"], p["hidden"]
        raw = [latest[u] for u in p["uids"] if u in latest]
        if raw:                                    # 판정은 0.5초마다지만 점은 지금 위치로
            x, y, hidden = round(raw[-1]["x"], 2), round(raw[-1]["y"], 2), False
        used.update(p["uids"])
        people.append({"id": p["name"], "x": x, "y": y, "hidden": hidden, "situation": p["situation"],
                       "prob": p["prob"], "abnormal": p["situation"] in ABNORMAL,
                       "still_s": p["still_s"], "elapsed_s": p["elapsed_s"]})
    for uid, r in latest.items():                  # 아직 판정에 안 들어간 새 사람
        if uid in used or r["n_points"] < C.MIN_POINTS:
            continue
        if REGION is not None and not bool(REGION.contains(r["x"], r["y"])):
            continue
        people.append({"id": f"T{uid}", "x": round(r["x"], 2), "y": round(r["y"], 2), "hidden": False,
                       "situation": C.UNDECIDED, "prob": 0.0, "abnormal": False, "still_s": 0.0, "elapsed_s": 0.0})
    return {"type": "frame", "t": datetime.now(TZ).isoformat(timespec="milliseconds"),
            "people": people, "radar": "ok" if rx.radar_state() == "ok" else rx.radar_state()}


async def broadcaster() -> None:
    """이벤트는 생기는 대로, 프레임은 초당 FPS 번 보낸다."""
    assert OUTBOX is not None
    period = 1 / FPS
    next_frame = time.monotonic()

    async def send(msg: dict) -> None:
        for ws in list(CLIENTS):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                CLIENTS.discard(ws)

    while True:
        while not OUTBOX.empty():
            await send(OUTBOX.get_nowait())
        wait = next_frame - time.monotonic()
        if wait > 0:
            try:
                await send(await asyncio.wait_for(OUTBOX.get(), wait))
                continue
            except asyncio.TimeoutError:
                pass
        next_frame += period
        if time.monotonic() - next_frame > 1:      # 오래 밀렸으면 따라잡지 말고 지금부터
            next_frame = time.monotonic()
        if CLIENTS:
            await send(build_frame())


async def housekeeping() -> None:
    while True:
        try:
            n = await asyncio.get_running_loop().run_in_executor(
                pool, store.cleanup, int(CFG["device_keep_days"]))
            if n:
                log.info("%d일 지난 이벤트 %d개 정리", CFG["device_keep_days"], n)
        except Exception:  # noqa: BLE001
            log.exception("정리 오류")
        await asyncio.sleep(3600)


# ------------------------------------------------------------------ mDNS

def start_mdns():
    if not CFG["mdns"]["enabled"]:
        return None
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        log.info("zeroconf 없음: 기기 자동 찾기 꺼짐")
        return None
    from radar import local_ipv4
    ip = local_ipv4()
    if not ip:
        return None
    host = socket.gethostname()
    info = ServiceInfo("_doorradar._tcp.local.", f"{CFG['device_name']}._doorradar._tcp.local.",
                       addresses=[socket.inet_aton(ip)], port=int(CFG["http_port"]),
                       properties={"host": f"{host}.local", "version": VERSION}, server=f"{host}.local.")
    zc = Zeroconf()
    zc.register_service(info)
    log.info("mDNS: _doorradar._tcp %s:%d (%s.local)", ip, CFG["http_port"], host)
    return zc, info


# ------------------------------------------------------------------ 앱

@asynccontextmanager
async def lifespan(_: FastAPI):
    global LOOP, OUTBOX
    LOOP = asyncio.get_running_loop()
    OUTBOX = asyncio.Queue()
    (DATA / "pair_code.txt").write_text(PAIR_CODE + "\n", encoding="utf-8")
    log.info("페어링 코드: %s  (%s)", PAIR_CODE, DATA / "pair_code.txt")
    log.info("모델: %s (판정 영역 %s)", model_path.name, REGION.text() if REGION else "없음")
    rx.start()
    judge.start()
    cloud.start()
    # Independent task: Cloud WebSocket delays must not block Radar/Judge or local WebSocket.
    cloud_live = CloudLiveClient(CFG["cloud"], build_frame, device_status_snapshot)
    tasks = [asyncio.create_task(broadcaster()), asyncio.create_task(housekeeping()),
             asyncio.create_task(cloud_live.run(), name="cloud-live")]
    mdns = await LOOP.run_in_executor(None, start_mdns)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        judge.stop()
        rx.stop()
        cloud.stop()
        cam.close()
        if mdns:
            zc, info = mdns
            zc.unregister_service(info)
            zc.close()


app = FastAPI(title="Door Radar Pi", version=VERSION, lifespan=lifespan)


@app.exception_handler(ApiError)
async def api_error(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)


def require_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not store.has_token(token):
        raise ApiError(401, "unauthorized", "토큰이 없거나 틀렸습니다. /api/pair 로 다시 페어링하세요.")
    return token


def region_dict() -> dict:
    if REGION is None:
        return {}
    return {"xmin": REGION.xmin, "xmax": REGION.xmax, "ymin": REGION.ymin, "ymax": REGION.ymax,
            "fov_deg": REGION.fov_deg}


def settings_view() -> dict:
    s = {k: store.settings[k] for k in ("notify", "camera_on", "camera_prob", "camera_hold_s")}
    return {**s, "region": region_dict(), "device_keep_days": int(CFG["device_keep_days"])}


def get_event(event_id: int) -> dict:
    ev = store.get(event_id)
    if ev is None:
        raise ApiError(404, "not_found", f"이벤트 {event_id} 가 없습니다.")
    return ev


@app.get("/api/info")
def info() -> dict:
    return {"name": CFG["device_name"], "host": f"{socket.gethostname()}.local", "version": VERSION,
            "model": model_path.stem, "model_version": f"v{C.MODEL_VERSION}", "region": region_dict()}


@app.post("/api/pair")
async def pair(request: Request) -> dict:
    body = await request.json()
    if str(body.get("code", "")).strip() != PAIR_CODE:
        raise ApiError(401, "bad_code", "페어링 코드가 틀렸습니다.")
    return {"token": store.new_token()}


def cpu_temp() -> float | None:
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        return None


def device_status_snapshot() -> dict:
    """Pi system stats shared by local REST status and Cloud LIVE status messages."""
    counts = store.counts()
    return {
        "radar": rx.radar_state(), "radar_host": rx.connected_host or CFG["radar"]["host"] or None,
        "radar_error": rx.error, "frame_rate": rx.frame_rate(),
        "camera": cam.state, "camera_backend": cam.backend,
        "latency_s": round(judge.analyze_s, 2), "judge_error": judge.error, "cpu_temp_c": cpu_temp(),
        "uptime_s": int(time.monotonic() - STARTED),
        "storage": {"used_gb": counts["used_gb"], "total_gb": counts["total_gb"], "photos": counts["photos"]},
    }


@app.get("/api/status")
def status(token: str = Depends(require_token)) -> dict:
    return {**device_status_snapshot(),
            "pending_photos": len(store.pending(token, int(CFG["device_keep_days"])))}


@app.post("/api/radar/reconnect")
def radar_reconnect(_: str = Depends(require_token)) -> dict:
    rx.reconnect()
    return {"radar": rx.radar_state()}


@app.get("/api/events")
def events(since: str | None = None, limit: int = Query(50, ge=1, le=500), abnormal: bool | None = None,
           _: str = Depends(require_token)) -> list[dict]:
    if since:
        try:
            s = datetime.fromisoformat(since.replace(" ", "+").replace("Z", "+00:00"))
        except ValueError:
            raise ApiError(400, "bad_since", "since 는 ISO 8601 시각이어야 합니다.")
        since = (s if s.tzinfo else s.replace(tzinfo=TZ)).astimezone(TZ).isoformat(timespec="milliseconds")
    return [summary(e) for e in store.list(since, limit, abnormal)]


@app.get("/api/events/{event_id}")
def event_detail(event_id: int, _: str = Depends(require_token)) -> dict:
    return get_event(event_id)


@app.get("/api/events/{event_id}/photo")
def event_photo(event_id: int, thumb: int = 0, _: str = Depends(require_token)):
    get_event(event_id)
    p = store.photo_file(event_id)
    if p is None or not p.exists():
        raise ApiError(404, "no_photo", "이 이벤트에는 사진이 없습니다.")
    if thumb and thumb_path(p).exists():
        p = thumb_path(p)
    return FileResponse(p, media_type="image/jpeg")


@app.post("/api/events/{event_id}/label")
async def event_label(event_id: int, request: Request, _: str = Depends(require_token)) -> dict:
    get_event(event_id)
    situation = (await request.json()).get("situation")     # null 이면 정답 지우기 (앱의 "되돌리기")
    if situation is not None and (situation not in SITUATIONS or situation == C.UNDECIDED):
        raise ApiError(400, "bad_situation", "situation 은 " + ", ".join(s for s in SITUATIONS if s != C.UNDECIDED)
                       + " 중 하나여야 합니다.")
    saved = store.set_label(event_id, situation)
    cloud.enqueue(event_id)
    return summary(saved)


@app.get("/api/settings")
def get_settings(_: str = Depends(require_token)) -> dict:
    return settings_view()


@app.put("/api/settings")
async def put_settings(request: Request, _: str = Depends(require_token)) -> dict:
    body = await request.json()
    new: dict = {}
    for key, value in body.items():
        if key in ("region", "device_keep_days"):
            continue                                   # 읽기 전용
        if key == "notify":
            if not isinstance(value, list) or not set(value) <= ABNORMAL:
                raise ApiError(400, "bad_notify", "notify 는 " + ", ".join(sorted(ABNORMAL)) + " 중에서 고릅니다.")
            new[key] = list(value)
        elif key == "camera_on":
            new[key] = bool(value)
        elif key == "camera_prob":
            if not 0 <= float(value) <= 1:
                raise ApiError(400, "bad_camera_prob", "camera_prob 는 0~1 입니다.")
            new[key] = float(value)
        elif key == "camera_hold_s":
            if not 0 <= float(value) <= 10:
                raise ApiError(400, "bad_camera_hold_s", "camera_hold_s 는 0~10초 입니다.")
            new[key] = float(value)
        else:
            raise ApiError(400, "unknown_setting", f"모르는 설정입니다: {key}")
    store.save_settings(new)
    return settings_view()


@app.get("/api/photos/pending")
def photos_pending(token: str = Depends(require_token)) -> dict:
    return {"ids": store.pending(token, int(CFG["device_keep_days"]))}


@app.post("/api/photos/ack")
async def photos_ack(request: Request, token: str = Depends(require_token)) -> dict:
    ids = (await request.json()).get("ids", [])
    return {"acked": store.ack(token, [int(i) for i in ids])}


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, token: str = "") -> None:
    if not store.has_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    CLIENTS.add(ws)
    try:
        await ws.send_json(build_frame())
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        CLIENTS.discard(ws)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(CFG["http_port"]), log_level="warning")
