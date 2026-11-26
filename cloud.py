"""파이 → 클라우드(rad2-cloud) 이벤트·사진 올리기.

로컬 서버는 그대로 두고, 이벤트가 생기거나 바뀔 때 id 만 보낼 목록(outbox)에 넣는다.
따로 도는 스레드가 그 id 의 *지금* 내용을 읽어 올린다. 그래서
  - 판정·촬영 스레드가 네트워크를 기다리지 않는다 (Render 가 잠들어 있어도 카메라가 안 멈춤).
  - 인터넷이 끊기거나 파이를 껐다 켜도 data_dir/cloud_outbox.db 에 남아 있다가 나중에 올라간다.
  - final 이 여러 번 갱신돼도 마지막 내용 한 번만 올라간다 (클라우드는 (device_id, edge_event_id) 로 덮어씀).

보내는 것 (헤더 X-Device-Key: <기기 키>). 자세한 형식은 docs/CLOUD_API.md
    POST {url}/api/device/events                                JSON (아래 payload)
    POST {url}/api/device/events/{edge_event_id}/photo          image/jpeg 원본
    POST {url}/api/device/events/{edge_event_id}/photo?thumb=1  image/jpeg 썸네일 (있으면)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger("cloud")

DETAIL_KEYS = ("end_situation", "behaviors", "reason", "path", "also", "track")
TOP_KEYS = ("kind", "person", "t", "started_at", "ended_at", "situation", "prob", "abnormal", "photo", "label")

def payload(device_id: str, ev: dict) -> dict:
    #기기 이벤트(ev)와 설정(device_id)으로부터 클라우드 전송용 payload를 조합한다."""
    is_final = ev.get("kind") == "final"
    details = {k: ev[k] for k in DETAIL_KEYS if k in ev} if is_final else {}

    return {
        "device_id": device_id,
        "edge_event_id": ev["id"],
        "kind": ev.get("kind"),
        "person": ev.get("person"),
        "t": ev.get("t"),
        "situation": ev.get("situation"),
        "prob": ev.get("prob"),
        "abnormal": bool(ev.get("abnormal", False)),
        "started_at": ev.get("started_at"),
        "ended_at": ev.get("ended_at"),
        "photo": bool(ev.get("photo", False)),
        "details": details,
    }


class CloudClient:
    def __init__(self, cfg: dict, data_dir: Path, get_event: Callable[[int], dict | None],
                 photo_file: Callable[[int], Path | None], thumb_of: Callable[[Path], Path]):
        self.url = str(cfg.get("url") or "").rstrip("/")
        self.key = str(cfg.get("device_key") or "")
        self.device_id = str(cfg.get("device_id") or "rad2pi")
        self.timeout = float(cfg.get("timeout_s", 30.0))     # Render 무료는 깨어나는 데 30초쯤 걸림
        self.enabled = bool(self.url)
        self._get, self._photo, self._thumb = get_event, photo_file, thumb_of
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_ok: float | None = None
        self.error: str | None = None
        self.db = sqlite3.connect(data_dir / "cloud_outbox.db", check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS outbox (
                event_id INTEGER PRIMARY KEY,
                tries INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0,
                ver INTEGER NOT NULL DEFAULT 0      
            );
            CREATE TABLE IF NOT EXISTS photos_sent (event_id INTEGER PRIMARY KEY);
        """)
        self.db.commit()

    # ---- 바깥에서 부르는 것
    def enqueue(self, event_id: int) -> None:
        #"""이 이벤트를 (다시) 올려야 한다. 이미 목록에 있으면 바로 보내도록 당긴다."""
        if not self.enabled:
            return
        with self._lock:
            self.db.execute("INSERT INTO outbox (event_id) VALUES (?) "
                            "ON CONFLICT(event_id) DO UPDATE SET next_at = 0, tries = 0, ver = ver + 1", (event_id,))
            self.db.commit()
        self._wake.set()

    def pending(self) -> int:
        with self._lock:
            return int(self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def start(self) -> None:
        if not self.enabled:
            log.info("클라우드 꺼짐 (config.toml [cloud] url 이 비어 있음)")
            return
        log.info("클라우드: %s (device_id=%s, 기기 키 %s, 밀린 이벤트 %d개)", self.url, self.device_id,
                 "있음" if self.key else "없음", self.pending())
        self._thread = threading.Thread(target=self._run, name="cloud", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ---- 스레드
    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                row = self.db.execute("SELECT event_id, tries, next_at, ver FROM outbox ORDER BY next_at, event_id "
                                      "LIMIT 1").fetchone()
            if row is None:
                self._wake.wait()
                self._wake.clear()
                continue
            event_id, tries, next_at, ver = row
            wait = next_at - time.time()
            if wait > 0:
                self._wake.wait(min(wait, 60))
                self._wake.clear()
                continue
            try:
                self._send(event_id)
            except Exception as exc:  # noqa: BLE001
                tries += 1
                delay = min(5 * 2 ** min(tries - 1, 7), 600)   # 5초, 10초 … 최대 10분
                self.error = str(exc)
                (log.warning if tries <= 3 or tries % 20 == 0 else log.debug)(
                    "클라우드 전송 실패 (이벤트 %d, %d번째, %d초 뒤 다시): %s", event_id, tries, delay, exc)
                with self._lock:
                    self.db.execute("UPDATE outbox SET tries=?, next_at=? WHERE event_id=? AND ver=?",
                                    (tries, time.time() + delay, event_id, ver))
                    self.db.commit()
                continue
            with self._lock:
                # 보내는 사이에 다시 enqueue 됐으면(ver 이 바뀜) 남겨 둬서 새 내용을 한 번 더 보낸다
                self.db.execute("DELETE FROM outbox WHERE event_id=? AND ver=?", (event_id, ver))
                self.db.commit()
            self.last_ok, self.error = time.time(), None

    # 2) 한 이벤트에 대해 JSON 전송 + 사진/썸네일 전송 순서 제어
    def _send(self, event_id: int) -> None:
        ev = self._get(event_id)
        if ev is None:                       # 파이에서 이미 지워짐 (보관 기간 지남)
            return
        body_dict = payload(self.device_id, ev)
        log.info("person situation: %s", body_dict.get("situation"))

        # 문서에 적힌 이벤트 전송 경로와 JSON 형식에 맞게 호출
        self._post(
            "/api/device/events",
            json.dumps(payload(self.device_id, ev), ensure_ascii=False).encode("utf-8"),
            "application/json",
        )

        if not ev.get("photo"):
            return

        with self._lock:
            if self.db.execute("SELECT 1 FROM photos_sent WHERE event_id=?", (event_id,)).fetchone():
                return

        p = self._photo(event_id)
        if p is None or not p.exists():
            return

        # 문서에 적힌 사진 업로드 경로/방식에 맞게 조정
        self._post(f"/api/device/events/{event_id}/photo", p.read_bytes(), "image/jpeg")
        t = self._thumb(p)
        if t.exists():
            self._post(f"/api/device/events/{event_id}/photo?thumb=1", t.read_bytes(), "image/jpeg")

        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO photos_sent VALUES (?)", (event_id,))
            self.db.commit()
        log.info("클라우드: 이벤트 %d 사진 올림", event_id)

    # 3) 인증 헤더 및 HTTP 요청 전송
    def _post(self, path: str, body: bytes, ctype: str) -> None:
        headers = {"Content-Type": ctype}
        if self.key:
            # 문서의 인증 헤더 이름(예: X-Device-Key 또는 Authorization) 확인
            headers["X-Device-Key"] = self.key
        req = urllib.request.Request(self.url + path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            raise RuntimeError(f"{path} → HTTP {exc.code} {detail}") from None
