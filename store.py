"""이벤트·토큰·받아 간 사진 기록 (SQLite) 과 설정 (settings.json).

data_dir/
    doorradar.db
    settings.json
    pair_code.txt
    photos/<날짜>/<이벤트 id>.jpg, <id>_thumb.jpg
    rows/<날짜>/<이벤트 id>.json.gz     최종 판정에 쓴 행 (나중에 정답과 함께 학습 데이터로 내보내기)
"""
from __future__ import annotations

import gzip
import json
import logging
import secrets
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- camera | final
    person_key TEXT NOT NULL,        -- 부팅 id + 사람 키 (최종 판정은 사람마다 하나, 갱신됨)
    t TEXT NOT NULL,                 -- ISO, 정렬·since 기준
    abnormal INTEGER NOT NULL,
    data TEXT NOT NULL,              -- JSON (id, photo 는 읽을 때 넣음)
    photo TEXT                       -- data_dir 기준 상대 경로
);
CREATE INDEX IF NOT EXISTS events_t ON events(t);
CREATE UNIQUE INDEX IF NOT EXISTS events_final ON events(person_key) WHERE kind = 'final';
CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS acks (token TEXT NOT NULL, event_id INTEGER NOT NULL, PRIMARY KEY (token, event_id));
"""

DEFAULT_SETTINGS = {
    "notify": ["장시간 체류", "배회", "반복 접근"],
    "camera_on": True,
    "camera_prob": 0.6,
    "camera_hold_s": 1.0,
}


class Store:
    def __init__(self, data_dir: Path, tz):
        self.dir = data_dir
        self.tz = tz
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "photos").mkdir(exist_ok=True)
        (self.dir / "rows").mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.dir / "doorradar.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._settings_path = self.dir / "settings.json"
        self.settings = dict(DEFAULT_SETTINGS)
        if self._settings_path.exists():
            try:
                self.settings.update(json.loads(self._settings_path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                log.warning("settings.json 을 못 읽음 (기본값 사용): %s", exc)

    # ---- 설정
    def save_settings(self, new: dict) -> dict:
        with self._lock:
            self.settings.update(new)
            tmp = self._settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._settings_path)
            return dict(self.settings)

    # ---- 토큰
    def new_token(self) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self.db.execute("INSERT INTO tokens VALUES (?, ?)", (token, datetime.now(self.tz).isoformat()))
            self.db.commit()
        return token

    def has_token(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            return self.db.execute("SELECT 1 FROM tokens WHERE token = ?", (token,)).fetchone() is not None

    # ---- 이벤트
    def photo_path(self, event_id: int, t: datetime) -> Path:
        return self.dir / "photos" / t.strftime("%Y-%m-%d") / f"{event_id}.jpg"

    def reserve_id(self) -> int:
        """사진 파일 이름에 쓸 id 를 먼저 받는다 (빈 camera 이벤트를 넣어 두고 나중에 채움)."""
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO events (kind, person_key, t, abnormal, data) VALUES ('camera', '', '', 1, '{}')")
            self.db.commit()
            return int(cur.lastrowid)

    def put_camera(self, event_id: int, person_key: str, ev: dict, photo: Path | None) -> dict:
        rel = str(photo.relative_to(self.dir)) if photo else None
        with self._lock:
            self.db.execute("UPDATE events SET person_key=?, t=?, abnormal=?, data=?, photo=? WHERE id=?",
                            (person_key, ev["t"], int(ev["abnormal"]), json.dumps(ev, ensure_ascii=False),
                             rel, event_id))
            self.db.commit()
        return self.get(event_id)

    def drop(self, event_id: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            self.db.commit()

    def camera_photo_of(self, person_key: str) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT photo FROM events WHERE kind='camera' AND person_key=? AND photo IS NOT NULL "
                                "ORDER BY id LIMIT 1", (person_key,)).fetchone()
        return r["photo"] if r else None

    def upsert_final(self, person_key: str, ev: dict, rows: list[dict] | None) -> tuple[dict, bool]:
        """사람마다 최종 판정 하나. 이미 있으면 갱신 (started_at 은 처음 값 유지). (이벤트, 새로 만들었는지)"""
        photo = self.camera_photo_of(person_key)
        with self._lock:
            old = self.db.execute("SELECT id, data FROM events WHERE kind='final' AND person_key=?",
                                  (person_key,)).fetchone()
            if old:
                prev = json.loads(old["data"])
                ev["started_at"] = min(prev.get("started_at") or ev["started_at"], ev["started_at"])
                ev["label"] = prev.get("label")
                self.db.execute("UPDATE events SET t=?, abnormal=?, data=?, photo=? WHERE id=?",
                                (ev["t"], int(ev["abnormal"]), json.dumps(ev, ensure_ascii=False), photo, old["id"]))
                event_id, created = int(old["id"]), False
            else:
                cur = self.db.execute(
                    "INSERT INTO events (kind, person_key, t, abnormal, data, photo) VALUES ('final', ?, ?, ?, ?, ?)",
                    (person_key, ev["t"], int(ev["abnormal"]), json.dumps(ev, ensure_ascii=False), photo))
                event_id, created = int(cur.lastrowid), True
            self.db.commit()
        if rows:
            p = self.dir / "rows" / ev["t"][:10] / f"{event_id}.json.gz"
            p.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(p, "wt", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, allow_nan=True)
        return self.get(event_id), created

    def _event(self, r: sqlite3.Row) -> dict:
        ev = json.loads(r["data"])
        ev["id"] = int(r["id"])
        ev["kind"] = r["kind"]
        ev["photo"] = bool(r["photo"])
        return ev

    def get(self, event_id: int) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM events WHERE id = ? AND t != ''", (event_id,)).fetchone()
        return self._event(r) if r else None

    def photo_file(self, event_id: int) -> Path | None:
        with self._lock:
            r = self.db.execute("SELECT photo FROM events WHERE id = ?", (event_id,)).fetchone()
        return (self.dir / r["photo"]) if r and r["photo"] else None

    def list(self, since: str | None, limit: int, abnormal: bool | None) -> list[dict]:
        q, args = "SELECT * FROM events WHERE t != ''", []
        if since:
            q += " AND t > ?"
            args.append(since)
        if abnormal is not None:
            q += " AND abnormal = ?"
            args.append(int(abnormal))
        q += " ORDER BY t DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.db.execute(q, args).fetchall()
        return [self._event(r) for r in rows]

    def set_label(self, event_id: int, situation: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT data FROM events WHERE id = ?", (event_id,)).fetchone()
            if not r:
                return None
            ev = json.loads(r["data"])
            ev["label"] = situation
            self.db.execute("UPDATE events SET data = ? WHERE id = ?", (json.dumps(ev, ensure_ascii=False), event_id))
            self.db.commit()
        return self.get(event_id)

    # ---- 사진 동기화
    def pending(self, token: str, keep_days: int) -> list[int]:
        since = (datetime.now(self.tz) - timedelta(days=keep_days)).isoformat()
        with self._lock:
            rows = self.db.execute(
                "SELECT id FROM events WHERE photo IS NOT NULL AND t > ? AND id NOT IN "
                "(SELECT event_id FROM acks WHERE token = ?) ORDER BY t", (since, token)).fetchall()
        return [int(r["id"]) for r in rows]

    def ack(self, token: str, ids: list[int]) -> int:
        with self._lock:
            self.db.executemany("INSERT OR IGNORE INTO acks VALUES (?, ?)", [(token, int(i)) for i in ids])
            self.db.commit()
            return self.db.execute("SELECT COUNT(*) FROM acks WHERE token = ?", (token,)).fetchone()[0]

    # ---- 정리
    def cleanup(self, keep_days: int) -> int:
        """keep_days 보다 오래된 이벤트·사진·행 삭제. 지운 이벤트 수."""
        cut = datetime.now(self.tz) - timedelta(days=keep_days)
        with self._lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM events WHERE t != '' AND t < ?",
                                                  (cut.isoformat(),)).fetchall()]
            self.db.execute("DELETE FROM events WHERE t != '' AND t < ?", (cut.isoformat(),))
            if ids:
                q = ",".join("?" * len(ids))
                self.db.execute(f"DELETE FROM acks WHERE event_id IN ({q})", ids)
            self.db.execute("DELETE FROM events WHERE t = '' AND id < (SELECT MAX(id) - 100 FROM events)")
            self.db.commit()
        for sub in ("photos", "rows"):
            for d in (self.dir / sub).iterdir():
                if d.is_dir() and d.name < cut.strftime("%Y-%m-%d"):
                    shutil.rmtree(d, ignore_errors=True)
        return len(ids)

    def counts(self) -> dict[str, Any]:
        with self._lock:
            photos = self.db.execute("SELECT COUNT(*) FROM events WHERE photo IS NOT NULL").fetchone()[0]
            events = self.db.execute("SELECT COUNT(*) FROM events WHERE t != ''").fetchone()[0]
        usage = shutil.disk_usage(self.dir)
        return {"photos": int(photos), "events": int(events),
                "used_gb": round((usage.total - usage.free) / 1e9, 1), "total_gb": round(usage.total / 1e9, 1)}
