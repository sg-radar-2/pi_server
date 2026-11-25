"""카메라 촬영 (Camera Module 3 Wide NoIR).

서버 venv 의 numpy(2.x, 모델 파일 때문에 필요)와 시스템 picamera2 가 쓰는 numpy 가 다를 수 있어서,
picamera2 는 시스템 python3 로 도는 작은 도우미(camera_helper.py)에서 쓴다. 도우미는 카메라를 켜 둔 채로
"capture <경로>" 를 받을 때마다 바로 찍어서 촬영이 빠르다.

backend
    auto     도우미 → rpicam-still → 없음 순서로 시도
    helper   picamera2 도우미만
    rpicam   rpicam-still 명령 (매번 카메라를 켜서 1~2초 걸림)
    fake     회색 그림 (PC 에서 개발할 때)
    none     촬영 안 함
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger("camera")

HERE = Path(__file__).resolve().parent
THUMB_SIZE = (320, 180)


class Camera:
    def __init__(self, backend: str = "auto", width: int = 1920, height: int = 1080,
                 helper_python: str = "/usr/bin/python3"):
        self.width, self.height = width, height
        self.helper_python = helper_python
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self.backend = self._choose(backend)
        self.error: str | None = None
        log.info("카메라: %s", self.backend)

    # ---- 고르기
    def _choose(self, want: str) -> str:
        if want in ("fake", "none"):
            return want
        if want in ("auto", "helper") and self._start_helper():
            return "helper"
        if want == "helper":
            return "none"
        if want in ("auto", "rpicam") and shutil.which("rpicam-still"):
            return "rpicam"
        return "none"

    def _start_helper(self) -> bool:
        if not Path(self.helper_python).exists():
            return False
        try:
            self._proc = subprocess.Popen(
                [self.helper_python, str(HERE / "camera_helper.py"), str(self.width), str(self.height)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            ready = self._proc.stdout.readline().strip()
            if ready == "ready":
                return True
            log.warning("카메라 도우미 시작 실패: %s", ready or "응답 없음")
        except OSError as exc:
            log.warning("카메라 도우미 실행 실패: %s", exc)
        self._kill_helper()
        return False

    def _kill_helper(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None

    # ---- 촬영
    @property
    def state(self) -> str:
        if self.backend == "none":
            return "none"
        return "error" if self.error else "ok"

    def capture(self, path: Path) -> bool:
        """사진을 path 에 JPEG 로 저장하고 썸네일(<이름>_thumb.jpg)도 만든다. 성공하면 True."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            ok = False
            try:
                if self.backend == "helper":
                    ok = self._capture_helper(path)
                    if not ok and shutil.which("rpicam-still"):
                        log.warning("도우미 촬영 실패 → rpicam-still 로 바꿈")
                        self._kill_helper()
                        self.backend = "rpicam"
                        ok = self._capture_rpicam(path)
                elif self.backend == "rpicam":
                    ok = self._capture_rpicam(path)
                elif self.backend == "fake":
                    ok = self._capture_fake(path)
            except Exception as exc:  # noqa: BLE001
                log.exception("촬영 오류")
                self.error = str(exc)
                return False
            self.error = None if ok else "촬영 실패"
            if ok:
                make_thumb(path)
            return ok

    def _capture_helper(self, path: Path) -> bool:
        if self._proc is None or self._proc.poll() is not None:
            if not self._start_helper():
                return False
        self._proc.stdin.write(f"capture {path}\n")
        self._proc.stdin.flush()
        reply = self._proc.stdout.readline().strip()
        if reply != "ok":
            log.warning("도우미: %s", reply or "응답 없음")
        return reply == "ok" and path.exists()

    def _capture_rpicam(self, path: Path) -> bool:
        r = subprocess.run(["rpicam-still", "-n", "-t", "1", "--width", str(self.width),
                            "--height", str(self.height), "-o", str(path)],
                           capture_output=True, timeout=15)
        if r.returncode != 0:
            log.warning("rpicam-still: %s", r.stderr.decode(errors="replace")[-300:])
        return r.returncode == 0 and path.exists()

    def _capture_fake(self, path: Path) -> bool:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (self.width, self.height), (40, 40, 40))
        ImageDraw.Draw(img).text((40, 40), f"FAKE CAMERA {path.stem}", fill=(220, 220, 220))
        img.save(path, "JPEG", quality=85)
        return True

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.stdin.write("quit\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._kill_helper()


def thumb_path(path: Path) -> Path:
    return path.with_name(path.stem + "_thumb.jpg")


def make_thumb(path: Path) -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    try:
        with Image.open(path) as img:
            img.thumbnail(THUMB_SIZE)
            img.convert("RGB").save(thumb_path(path), "JPEG", quality=80)
    except OSError as exc:
        log.warning("썸네일 실패: %s", exc)


if __name__ == "__main__":  # 시험: python camera.py test.jpg
    logging.basicConfig(level=logging.INFO)
    cam = Camera(sys.argv[2] if len(sys.argv) > 2 else "auto")
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "test.jpg").resolve()
    print(cam.backend, cam.capture(out), out)
    cam.close()
