"""실시간 판정 (situation_viewer.pyw 의 실시간 모드를 화면 없이 옮긴 것).

0.5초마다 수신기의 최근 행으로 situation_core.analyze 를 돌리고, 사람(이어 붙인 트랙)마다
  - 진행 중 판정과 위치  → 실시간 프레임
  - 새 카메라 촬영       → on_camera(사람, 촬영)
  - 사라지고 FINAL_DELAY_S 지남 → on_final(사람)   (나중에 같은 사람으로 다시 이어지면 다시 호출 = 갱신)
을 만든다.

사람 키: 그 사람에게 이어 붙은 등장 번호(uid) 중 가장 작은 것. 수신기가 레이더 ID를 등장마다 새 번호로 바꿔 주므로
몇 분 동안 바뀌지 않는다.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable

import situation_core as C

log = logging.getLogger("judge")

FINAL_DELAY_S = 3.0      # 사라지고 이만큼 지나야 최종 판정을 낸다 (잠깐 놓친 것과 구분)
GONE_S = 0.5             # situation_core 와 같은 기준: 마지막 행이 지금보다 이만큼 앞이면 사라짐
TRACK_STEP_S = 0.2       # 이벤트에 넣는 궤적 간격

SOURCE = {"진행 중": "progress", "끝날 때": "end"}


def _num(v: float, nd: int = 2) -> float:
    v = float(v)
    return round(v, nd) if math.isfinite(v) else 0.0


def person_key(tr) -> int:
    return int(min(tr.target_ids or [tr.target_id]))


def summarize(tr, now_t: float, region) -> dict:
    """트랙 하나 → 서버가 쓰는 사람 정보 (JSON으로 바로 보낼 수 있는 값만)"""
    ix = C.SITUATION_FEATURES.index
    last = tr.sfeat[-1]
    hidden = bool(tr.hidden[-1]) if tr.hidden is not None and len(tr.hidden) else False
    # 궤적: TRACK_STEP_S 간격으로 솎음
    track, next_t = [], -1.0
    for t, x, y in zip(tr.t, tr.xs, tr.ys):
        if t >= next_t:
            track.append([_num(t, 1), _num(x), _num(y)])
            next_t = t + TRACK_STEP_S
    behaviors = [{"situation": b["situation"], "prob": _num(b["prob"]),
                  "source": SOURCE.get(b["source"], b["source"])} for b in getattr(tr, "behaviors", [])]
    return {
        "key": person_key(tr),
        "name": C.track_name(tr),
        "uids": [int(u) for u in (tr.target_ids or [tr.target_id])],
        "t0": float(tr.t_abs[0]),
        "t1": float(tr.t_abs[-1]),
        "ended": bool(tr.t_abs[-1] < now_t - GONE_S),
        "x": _num(tr.xs[-1]), "y": _num(tr.ys[-1]), "hidden": hidden,
        "situation": str(tr.sit_pred[-1]), "prob": _num(tr.sit_prob[-1]),
        "still_s": _num(last[ix("cur_still_s")], 1), "elapsed_s": _num(tr.t[-1], 1),
        "final": {
            "situation": str(tr.final_pred), "prob": _num(tr.final_prob),
            "also": list(getattr(tr, "final_also", []) or []),
            "end_situation": str(getattr(tr, "end_pred", tr.final_pred)),
            "behaviors": behaviors,
            "reason": getattr(tr, "rule_note", "") or "",
            "path": C.describe_path(tr, region),
        },
        "cameras": [dict(c) for c in tr.cameras],
        "track": track,
    }


class LiveJudge:
    def __init__(self, receiver, models, settings: Callable[[], dict],
                 on_camera: Callable[[dict, dict], None], on_final: Callable[[dict], None],
                 interval_s: float = 0.5, final_delay_s: float = FINAL_DELAY_S):
        self.rx = receiver
        self.models = models
        self.region = C.model_region(models)
        self.settings = settings
        self.on_camera = on_camera
        self.on_final = on_final
        self.interval_s = interval_s
        self.final_delay_s = final_delay_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._done_ver = -1
        self._cache: dict[int, dict] = {}        # 사람 키 → 마지막 정보
        self._fired: dict[tuple, float] = {}     # (사람 키, 상황) → 촬영 시각
        self._finalized: dict[int, float] = {}   # 사람 키 → 최종 판정을 낸 t1
        self.people: list[dict] = []             # 지금 범위 안에 있는 사람들 (사라지지 않은)
        self.analyze_s = 0.0
        self.error: str | None = None
        self.last_run = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="judge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            t_start = time.monotonic()
            try:
                self.tick()
                self.error = None
            except Exception as exc:  # noqa: BLE001 - 판정 오류로 서버가 멈추면 안 됨
                self.error = f"{type(exc).__name__}: {exc}"
                log.exception("판정 오류")
            spent = time.monotonic() - t_start
            self._stop.wait(max(self.interval_s - spent, 0.05))

    def tick(self) -> None:
        rows, ver = self.rx.snapshot()
        now_t = self.rx.now()
        if ver == self._done_ver:
            # 새 프레임이 없어도 시간은 흐르므로, 사라진 사람의 최종 판정 대기는 계속 확인
            self._finalize_waiting(now_t)
            return
        self._done_ver = ver
        s = self.settings()
        C.CAMERA_PROB = float(s.get("camera_prob", C.CAMERA_PROB))
        C.CAMERA_HOLD_S = float(s.get("camera_hold_s", C.CAMERA_HOLD_S))

        t0 = time.perf_counter()
        raw = C.tracks_from_rows(rows) if rows else []
        for tr in raw:
            tr.session_end = now_t          # 지금 시각이 '녹화 끝': 사라진 사람을 사라졌다고 봄
        result = C.analyze(raw, self.models) if raw else []
        self.analyze_s = time.perf_counter() - t0
        self.last_run = time.monotonic()
        if self.analyze_s > 1.0:
            log.warning("판정에 %.2f초 걸림 (window_s 를 줄이는 것을 고려)", self.analyze_s)

        seen: set[int] = set()
        active: list[dict] = []
        for tr in result:
            if getattr(tr, "out_of_range", False):
                continue
            info = summarize(tr, now_t, self.region)
            key = info["key"]
            seen.add(key)
            self._cache[key] = info
            for cam in info["cameras"]:
                ck = (key, cam["situation"])
                if ck not in self._fired:
                    self._fired[ck] = cam["t"]
                    self.on_camera(info, cam)
            if not info["ended"]:
                active.append(info)
        # 판정 창(window_s) 밖으로 밀려나 결과에서 빠진 사람: 마지막 정보로 최종 판정
        for key in [k for k in self._cache if k not in seen]:
            info = self._cache.pop(key)
            if self._finalized.get(key) != info["t1"]:
                info["ended"] = True
                self._finalized[key] = info["t1"]
                self.on_final(info)
        self._finalize_waiting(now_t)
        with self._lock:
            self.people = active
        self._prune(now_t)

    def _finalize_waiting(self, now_t: float) -> None:
        for key, info in list(self._cache.items()):
            if not info["ended"] or now_t - info["t1"] < self.final_delay_s:
                continue
            if self._finalized.get(key) == info["t1"]:
                continue                       # 이미 냈고 그 뒤로 바뀐 게 없음
            self._finalized[key] = info["t1"]
            self.on_final(info)

    def _prune(self, now_t: float) -> None:
        horizon = now_t - 2 * self.rx.window_s
        for k in [k for k, t in self._fired.items() if t < horizon]:
            del self._fired[k]
        for k in [k for k, t in self._finalized.items() if t < horizon]:
            del self._finalized[k]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.people)
