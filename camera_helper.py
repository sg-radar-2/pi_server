"""카메라 도우미: 시스템 python3 (picamera2 가 깔린 쪽)로 실행한다. camera.py 가 띄운다.

    /usr/bin/python3 camera_helper.py 1920 1080
    stdin:  capture /home/radar/doorradar-data/photos/2026-11-19/128.jpg
    stdout: ok | err <이유>
처음에 카메라를 켜고 "ready" 를 한 줄 쓴다. "quit" 이면 끝낸다.
"""
import sys
import time


def main() -> None:
    width = int(sys.argv[1]) if len(sys.argv) > 1 else 1920
    height = int(sys.argv[2]) if len(sys.argv) > 2 else 1080
    try:
        from picamera2 import Picamera2
        cam = Picamera2()
        cam.configure(cam.create_still_configuration(main={"size": (width, height)}))
        cam.start()
        time.sleep(1.0)                      # 노출·화이트밸런스가 자리 잡을 시간
    except Exception as exc:  # noqa: BLE001
        print(f"err {type(exc).__name__}: {exc}", flush=True)
        return
    print("ready", flush=True)
    for line in sys.stdin:
        cmd, _, arg = line.strip().partition(" ")
        if cmd == "quit":
            break
        if cmd != "capture" or not arg:
            print("err bad command", flush=True)
            continue
        try:
            cam.capture_file(arg)
            print("ok", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"err {type(exc).__name__}: {exc}", flush=True)
    cam.stop()
    cam.close()


if __name__ == "__main__":
    main()
