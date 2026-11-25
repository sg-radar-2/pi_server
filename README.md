# 도어 레이더 파이 서버

라즈베리파이에서 도는 서버. 레이더 데이터를 받아 판정하고, 이상 상황이면 사진을 찍고, 앱에 보낸다.

```
RETINA-4SN ──TCP 29172──▶ radar.py ──행(rows)──▶ judge.py ──▶ server.py ──REST/WS 8080──▶ 앱
 (공유기 Wi-Fi)            패킷 해석            situation_core      │  SQLite, 사진
                                               0.5초마다 판정      └─ camera.py ── Camera Module 3
```

| 파일 | 하는 일 |
| --- | --- |
| `radar.py` | 레이더 TCP 수신, 패킷 해석(`radar-studio/src/device/retina.cpp` 그대로), 끊기면 다시 연결, 주소를 모르면 같은 대역에서 찾기 |
| `judge.py` | `situation_viewer.pyw` 실시간 모드를 화면 없이: `tracks_from_rows` → `analyze` → 사람별 진행 중 판정, 촬영, 최종 판정 |
| `camera.py`, `camera_helper.py` | 촬영. picamera2 는 시스템 파이썬 도우미 프로세스에서 (numpy 버전 충돌 피하기), 안 되면 `rpicam-still` |
| `store.py` | 이벤트·토큰·받아 간 사진 (SQLite), 설정, 30일 지난 것 정리 |
| `server.py` | 앱 API (`door_radar_app/docs/SPEC.md`), WebSocket 초당 5프레임, mDNS `_doorradar._tcp` |
| `fake_radar.py` | 가짜 레이더 (개발용). `--replay`로 실제 녹화(트랙 CSV)를 레이더 패킷으로 재생 |
| `judge_file.py` | 녹화 파일을 바로 판정 (레이더 없이 알고리즘·속도 확인) |
| `deploy.ps1`, `install.sh`, `doorradar.service` | Windows → 파이 배포, 설치, 자동 시작 |

판정 코드는 복사하지 않고 `tools/behavior`의 `situation_core.py`를 그대로 쓴다. 배포할 때만 `behavior/`로 같이 올라간다.

## PC에서 먼저 시험 (레이더·파이 없이)

Python 3.11 이상. 판정 코드(`tools/behavior`)는 `./behavior`, `../behavior`, `../sg-radar-tools/tools/behavior` 순서로 자동으로 찾는다 (`Capstone\pi_server`에 두면 마지막 것).

```powershell
cd C:\Users\HGray\Desktop\SG\4th\Capstone\pi_server
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt

# 창 1: 가짜 레이더 (방문 → 장시간 체류 → 통과 반복)
python fake_radar.py
# 창 2: 서버 (레이더 = 내 PC, 카메라 = 회색 가짜 사진)
python server.py --radar 127.0.0.1 --camera fake
```

- 실제 녹화로 시험하려면 `fake_radar.py` 대신 `radar-emulator`로 `.pcr`을 재생한다(같은 29172 포트).
- 앱은 `door_radar_app`의 가짜 서버 대신 이 서버(`<PC IP>:8080`)에 붙이면 된다. 페어링 코드는 서버 창에 나온다.

## 레이더 없이 파이에서 판정 시험

레이더가 아직 안 붙어도, 지금까지 녹화한 데이터로 파이가 판정을 제대로 하는지 볼 수 있다.

```powershell
# [PC] 녹화 CSV까지 같이 올리기
powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -Pi radar@<파이IP> -WithRecordings
```

```bash
# [파이] 1) 녹화 파일을 바로 판정 (정답 표와 비교, 녹화 하나당 걸린 시간)
cd ~/doorradar
.venv/bin/python judge_file.py recordings/ --labels recordings/labels_1111.csv
.venv/bin/python judge_file.py recordings/6-3_tracks_raw.csv --detail      # 사람별 근거·촬영 시점

# [파이] 2) 녹화를 레이더처럼 재생해서 실제 서버로 실시간 판정 (앱까지 연결해 볼 수 있음)
sudo systemctl stop doorradar
.venv/bin/python fake_radar.py --replay recordings/6-3_tracks_raw.csv recordings/7-4_tracks_raw.csv &
.venv/bin/python server.py --radar 127.0.0.1
#   로그에 "촬영 이벤트", "최종 판정"이 찍힘. 끝내기: Ctrl+C, 그다음 kill %1 ; sudo systemctl start doorradar
```

- `judge_file.py`의 시간은 녹화 전체(최대 1~2분)를 한 번에 판정한 시간이다. 실시간 서버는 최근 180초를 0.5초마다 판정하므로, 이 값이 0.5초보다 충분히 작으면 Pi 4에서도 실시간으로 돈다.
- 재생은 녹화 사이를 40초 쉰다(`--gap`). 30초보다 짧으면 서버가 앞 녹화와 다음 녹화의 사람을 같은 사람으로 이어 붙인다.

## 파이에 올리기

### 처음 한 번: 비밀번호 없이 접속 (안 하면 배포할 때마다 비밀번호를 두 번 묻는다)

```powershell
ssh-keygen -t ed25519            # 이미 있으면 건너뜀. 질문은 전부 Enter
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh radar@rad2pi.local "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

### 배포 (코드나 모델을 바꿀 때마다)

```powershell
cd C:\Users\HGray\Desktop\SG\4th\Capstone\pi_server
powershell -ExecutionPolicy Bypass -File .\deploy.ps1                         # rad2pi.local 이 되는 곳
powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -Pi radar@10.1.160.100  # 학교 Wi-Fi 등 이름이 안 될 때 IP로
```

`~/doorradar`에 올리고 `install.sh`를 실행한다. 이 스크립트는 다음을 한다.

- 처음이면 apt 패키지(`python3-venv python3-picamera2 python3-pil`)를 설치한다.
- venv를 만들고 패키지를 설치한다.
- `config.toml`이 없으면 만든다.
- 시간대를 Asia/Seoul로 맞춘다.
- 모델이 열리는지 확인한다.
- systemd 서비스를 등록하고 재시작한다.

마지막 줄에 **페어링 코드**와 앱에 넣을 주소가 나온다. 파이의 `config.toml`은 덮어쓰지 않는다.

### 파이에서 자주 쓰는 명령 (`ssh radar@rad2pi.local` 접속 후)

```bash
journalctl -u doorradar -f              # 로그 보기 (레이더 연결, 촬영, 최종 판정)
cat ~/doorradar-data/pair_code.txt      # 페어링 코드
sudo systemctl restart doorradar        # 설정 바꾼 뒤
sudo systemctl stop doorradar           # 멈춤 (부팅하면 다시 켜짐)
nano ~/doorradar/config.toml            # 설정
curl -s localhost:8080/api/info         # 서버 살아 있는지
```

## 설정 (`config.toml`)

`config.example.toml`에 설명이 있다. 시연 전에 바꿀 만한 것만 적는다.

- `radar.host`: 레이더 IP. 비우면 켤 때 같은 대역(/24)을 훑어서 29172 포트가 열린 기기를 찾는다(몇 초 걸림). 공유기에서 레이더 IP를 고정하고 적어 두는 게 확실하다.
- `pair_code`: 시연 때는 고정해 두면 편하다(예: `"482913"`).
- `model`: 기본값은 `models/situation_trees_real.pkl`이다.
- `camera.backend`: 보통 `auto`. 촬영이 이상하면 `rpicam`으로 바꿔서 비교한다.

## 모델 파일

- 모델은 **지금 `situation_core.py`의 모델 버전(5)**으로 학습한 것이어야 열린다.
  - 지금 `models/` 안에서 버전 5는 `situation_trees_real.pkl`뿐이다.
  - `situation_trees_1111.pkl`은 버전 3이라 열리지 않는다.
- 설정한 모델이 안 맞으면, 서버는 `models/`에서 맞는 것 중 가장 최근 파일을 대신 쓰고 로그에 경고를 남긴다.
- `.pkl`은 학습한 PC와 **같은 scikit-learn 버전(지금 1.9.1)**이어야 열린다.
  - PC에서 scikit-learn을 올렸으면 `requirements.txt`도 같이 올린다.
  - 파이 파이썬에 그 버전이 안 깔리면, 양쪽을 깔리는 버전으로 맞추고 다시 학습한다.

## 앱 명세와 다른 점 / 더한 것

- **최종 판정 갱신:** 같은 사람이 사라졌다가 이어 붙으면 같은 `id`의 `final` 이벤트를 다시 보낸다.
  - 예: 외곽으로 나갔다 30초 안에 같은 자리로 다시 들어옴.
  - 앱은 `id` 기준으로 덮어쓴다.
- **`POST /api/radar/reconnect`:** 레이더 연결을 끊고 바로 다시 붙는다(기기 상태 화면의 "레이더 다시 연결").
- **`/api/status` 추가 필드:** `radar_host`, `radar_error`, `camera_backend`, `judge_error`, `pending_photos`
  - `radar` 값: `ok` / `connecting` / `disconnected` / `stale`(2초 넘게 프레임 없음)
- **실시간 프레임의 사람 id:** `T3+7`처럼 오는데, 숫자는 레이더 ID가 아니라 **등장 번호**다.
  - 같은 레이더 ID라도 5초 넘게 안 보였다 나오면 새 번호를 준다.
  - 레이더가 ID를 재사용해도 다른 사람이 섞이지 않게 하려는 것이다.
- **판정 전 사람도 보냄:** 아직 판정에 안 들어간 새 사람(잡힌 지 1초 안 됨)도 `판단 중`으로 보낸다. 앱 점이 바로 나타나게 하려는 것이다.
- **학습용 행 저장:** 최종 판정마다 그 사람의 행을 `~/doorradar-data/rows/<날짜>/<id>.json.gz`에 둔다.
  - 앱에서 정답을 표시한 이벤트를 학습 데이터로 내보내는 도구는 다음에 만든다.

## 문제 생길 때

| 증상 | 볼 것 |
| --- | --- |
| `레이더 찾는 중`만 반복 | 레이더가 같은 공유기에 붙었는지(레이더 관리 페이지 `:8000`에서 client 모드), `radar.host`에 IP 직접 적기 |
| `판정에 N초 걸림` 경고 | Pi 4에서 판정이 느림 → `radar.window_s`를 120 정도로 줄이기 |
| 사진이 안 찍힘 | `rpicam-hello --list-cameras`에 imx708이 나오는지, 로그의 `카메라:` 줄, `camera.backend = "rpicam"`으로 시험 |
| 앱이 못 붙음 | 폰이 같은 공유기 Wi-Fi인지, `curl http://rad2pi.local:8080/api/info`(PC에서), mDNS가 안 되면 IP로 |
| 모델 오류로 서버가 안 켜짐 | 위 "모델 파일" |
