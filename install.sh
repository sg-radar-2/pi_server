#!/usr/bin/env bash
# 라즈베리파이에서 실행: 패키지 설치, venv, systemd 서비스 등록·재시작.
# deploy.ps1 이 파일을 올린 뒤 자동으로 부른다. 직접 돌려도 됨:  cd ~/doorradar && bash install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$PWD"

echo "== 시스템 패키지"
need=()
for p in python3-venv python3-picamera2 python3-pil; do
  dpkg -s "$p" >/dev/null 2>&1 || need+=("$p")
done
if [ ${#need[@]} -gt 0 ]; then
  sudo apt-get update -y
  sudo apt-get install -y "${need[@]}"
fi

echo "== 파이썬 가상환경 ($(python3 --version))"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
if ! .venv/bin/pip install -q -r requirements.txt; then
  echo "!! 패키지 설치 실패. scikit-learn 버전이 이 파이썬에서 안 깔리면 requirements.txt 의 버전과"
  echo "   PC 의 버전을 함께 맞추고 모델을 다시 학습해야 합니다 (README '모델 파일' 참고)."
  exit 1
fi

[ -f config.toml ] || { cp config.example.toml config.toml; echo "== config.toml 만듦 (필요하면 고치세요)"; }

echo "== 시간대"
sudo timedatectl set-timezone Asia/Seoul || true

echo "== 판정 코드·모델 확인"
.venv/bin/python - <<'EOF'
import sys, tomllib, pathlib
sys.path.insert(0, "behavior")
import situation_core as C
cfg = tomllib.loads(pathlib.Path("config.toml").read_text(encoding="utf-8"))
m = pathlib.Path(cfg.get("behavior_dir") or "behavior") / cfg.get("model", "models/situation_trees_real.pkl")
C.load_models(m)
print(f"   {m} OK (모델 버전 {C.MODEL_VERSION})")
EOF

echo "== 카메라 확인"
if /usr/bin/python3 -c "import picamera2" 2>/dev/null; then echo "   picamera2 OK"; else echo "   picamera2 없음 (rpicam-still 로 촬영)"; fi
rpicam-hello --list-cameras 2>/dev/null | head -3 || true

echo "== 서비스 등록"
sed "s|__DIR__|$DIR|g; s|__USER__|$USER|g" doorradar.service | sudo tee /etc/systemd/system/doorradar.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable doorradar >/dev/null
sudo systemctl restart doorradar
sleep 4
systemctl --no-pager --lines=0 status doorradar | head -5 || true
echo
journalctl -u doorradar -n 15 --no-pager || true
echo
echo "페어링 코드: $(cat ~/doorradar-data/pair_code.txt 2>/dev/null || echo '(아직 없음 - journalctl -u doorradar -f 로 확인)')"
echo "앱 주소: http://$(hostname).local:8080  또는  http://$(hostname -I | awk '{print $1}'):8080"
