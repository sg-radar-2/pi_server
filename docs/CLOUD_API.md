# 파이 → 클라우드 API 구현 요청

파이(`pi_server/cloud.py`)는 이미 아래 형식으로 보내도록 만들어 두었습니다. 클라우드에서 이 두 API를 받아 주세요.

| | 경로 | 본문 |
|---|---|---|
| 1. 이벤트 | `POST /api/device/events` | JSON |
| 2. 사진 | `POST /api/device/events/{edge_event_id}/photo` | JPEG 원본 |
| | `POST /api/device/events/{edge_event_id}/photo?thumb=1` | JPEG 썸네일 |

공통 사항:

- 모든 요청에 `X-Device-Key: <Device API Key>` 헤더가 붙습니다.
  - 키가 틀리면 `401`을 주세요.
  - 키에 묶인 기기와 본문의 `device_id`가 다르면 `403`을 주세요.
- 처리했으면 `2xx`를 주세요. 본문은 자유입니다(예: `{"ok": true, "id": 클라우드 이벤트 id}`).
- 파이는 `2xx`가 아니면(타임아웃 포함) 같은 내용을 다시 보냅니다. 간격은 5초, 10초, … 최대 10분입니다.
  - 그래서 **같은 요청이 두 번 와도 결과가 같아야 합니다**.
  - 타임아웃은 30초입니다. Render가 잠에서 깨는 시간까지 기다립니다.

## 1. 이벤트 `POST /api/device/events`

```json
{
  "device_id": "rad2pi",
  "edge_event_id": 101,
  "kind": "final",
  "person": "T3+7",
  "t": "2026-11-25T20:20:41.500+09:00",
  "started_at": "2026-11-25T20:19:02.100+09:00",
  "ended_at": "2026-11-25T20:20:41.500+09:00",
  "situation": "배회",
  "prob": 0.87,
  "abnormal": true,
  "photo": true,
  "label": null,
  "details": {
    "end_situation": "통과",
    "also": [],
    "behaviors": [{"situation": "배회", "prob": 0.87, "source": "progress"}],
    "reason": "",
    "path": "왼쪽(2.1m) → 오른쪽(3.4m)",
    "track": [[0.0, -1.20, 2.31], [0.5, -1.05, 2.28]]
  }
}
```

### 필드

| 필드 | 형식 | 설명 | `events` 칸 |
|---|---|---|---|
| `device_id` | 문자열 | 기기 id | `device_id` |
| `edge_event_id` | 정수 | 파이 SQLite의 이벤트 id | `edge_event_id` |
| `kind` | `"camera"` / `"final"` | `camera`는 촬영 순간 이벤트, `final`은 사람이 떠난 뒤의 최종 판정 | `kind` |
| `person` | 문자열 | 사람 이름(예: `T3`, `T3+7`) | `person` |
| `t` | ISO 8601 (+09:00) | camera는 촬영 시각, final은 마지막으로 보인 시각 | `t` |
| `started_at` | ISO 8601 | 처음 보인 시각 | `started_at` |
| `ended_at` | ISO 8601 / `null` | 떠난 시각. camera는 항상 `null` | `ended_at` |
| `situation` | 문자열 | 판정 결과(아래 목록) | `situation` |
| `prob` | 0~1 | 확률 | `prob` |
| `abnormal` | bool | `situation`이 장시간 체류·배회·반복 접근이면 true | `abnormal` |
| `photo` | bool | 사진이 있으면 true. 이어서 2번 사진 API가 옵니다 | `photo` |
| `label` | 문자열 / `null` | 사용자가 앱에서 고른 정답. `null`은 정답 없음(되돌리기 포함) | `label` |
| `details` | 객체 | camera는 `{}`, final은 아래 내용 | `details` |

`situation`, `label` 값: `통과` `귀가` `외출` `방문` `배달` `장시간 체류` `배회` `반복 접근`. 이 중 마지막 세 개가 이상 상황입니다.

final의 `details`는 앱 상세 화면에 그대로 씁니다. 클라우드는 내용을 해석하지 말고 통째로 저장했다가 돌려주면 됩니다.

| `details` 키 | 설명 |
|---|---|
| `end_situation` | 떠날 때의 판정 |
| `also` | 같은 확률로 겹친 다른 이상 상황 목록 |
| `behaviors` | 판정이 바뀐 기록 `[{situation, prob, source}]` |
| `reason` | 규칙으로 판정을 고쳤을 때의 설명 |
| `path` | 들어오고 나간 방향 요약 |
| `track` | 궤적 `[[경과 초, x(m), y(m)], …]`. 수백 개가 될 수 있습니다 |

### 꼭 지켜 주실 것: 같은 id는 덮어쓰기

같은 `(device_id, edge_event_id)`가 여러 번 옵니다.

- final은 같은 사람이 다시 나타나면 같은 id로 내용이 바뀌어 다시 옵니다.
- 사용자가 정답을 고르거나 지우면 `label`만 바뀐 채로 다시 옵니다.
- 전송이 실패하면 같은 내용이 다시 옵니다.

그래서 `DO NOTHING`이 아니라 `DO UPDATE`로 저장해 주세요. 다만 `photo_key`는 사진 API가 채우는 값이니 덮어쓰지 않습니다.

```sql
INSERT INTO events (device_id, edge_event_id, kind, person, t, started_at, ended_at,
                    situation, prob, abnormal, photo, label, details)
VALUES (%(device_id)s, %(edge_event_id)s, %(kind)s, %(person)s, %(t)s, %(started_at)s, %(ended_at)s,
        %(situation)s, %(prob)s, %(abnormal)s, %(photo)s, %(label)s, %(details)s)
ON CONFLICT (device_id, edge_event_id) DO UPDATE SET
    kind = EXCLUDED.kind, person = EXCLUDED.person, t = EXCLUDED.t,
    started_at = EXCLUDED.started_at, ended_at = EXCLUDED.ended_at,
    situation = EXCLUDED.situation, prob = EXCLUDED.prob, abnormal = EXCLUDED.abnormal,
    photo = EXCLUDED.photo, label = EXCLUDED.label, details = EXCLUDED.details,
    updated_at = NOW()
RETURNING id;
```

`details`는 psycopg에서 `Jsonb(body["details"])`로 넘기면 됩니다.

같은 요청을 받을 때 `devices.last_seen_at = NOW()`도 갱신해 주세요. 앱의 파이 온라인/오프라인 표시에 씁니다.

## 2. 사진 `POST /api/device/events/{edge_event_id}/photo`

- 헤더: `X-Device-Key`, `Content-Type: image/jpeg`
- 본문: JPEG 바이트 그대로(multipart 아님). 원본은 1920×1080이고 수백 KB입니다.
- `?thumb=1`이면 썸네일입니다. 원본 다음에 옵니다.
- 순서: 파이는 항상 **이벤트(1번)가 성공한 다음에** 사진을 보냅니다.
  - 그래도 해당 이벤트가 없으면 `404`를 주세요. 파이가 나중에 이벤트부터 다시 보냅니다.
- 저장한 뒤 `events.photo_key`에 원본 위치를 적어 주세요. 썸네일 위치는 규칙으로 정하면 됩니다(예: `…/101.jpg`, `…/101_thumb.jpg`).
- 저장소(Render 디스크 / 오브젝트 스토리지 / PostgreSQL bytea)는 편한 것으로 골라 주세요.
  - Render 무료 디스크는 재배포하면 지워지니, 시연용이 아니면 오브젝트 스토리지나 DB를 추천합니다.
- 같은 사진이 다시 와도 덮어쓰면 됩니다.
- camera 이벤트와 그 사람의 final 이벤트는 같은 사진을 씁니다. 그래서 같은 JPEG가 서로 다른 `edge_event_id`로 두 번 올 수 있습니다.

## 알아 두실 것

- **`edge_event_id`와 클라우드 `events.id`는 다릅니다.**
  - 앱에는 클라우드 id를 주세요.
  - 나중에 클라우드가 파이에 명령(정답 표시 등)을 보낼 때는 `edge_event_id`로 바꿔서 보냅니다.
- **파이 DB를 지우면 `edge_event_id`가 1부터 다시 시작합니다.** 그때는 그 기기의 클라우드 이벤트도 지워 주세요. 재시작만으로는 id가 겹치지 않습니다.
- **보내는 것은 camera 이벤트, final 이벤트, 정답 변경입니다.** 판정 중인 실시간 위치(초당 5번)는 이 API로 보내지 않습니다. 실시간 위치는 나중에 WebSocket으로 따로 정하겠습니다.

## 확인용 curl

```bash
curl -X POST https://rad2-cloud.onrender.com/api/device/events \
  -H "X-Device-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"device_id":"rad2pi","edge_event_id":1,"kind":"camera","person":"T3","t":"2026-11-25T20:20:00.000+09:00","started_at":"2026-11-25T20:19:50.000+09:00","ended_at":null,"situation":"배회","prob":0.87,"abnormal":true,"photo":true,"label":null,"details":{}}'

curl -X POST https://rad2-cloud.onrender.com/api/device/events/1/photo \
  -H "X-Device-Key: $KEY" -H "Content-Type: image/jpeg" --data-binary @test.jpg
```
