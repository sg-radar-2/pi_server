"""녹화 파일(트랙 CSV)을 파이에서 판정해 본다. 레이더 없이 판정 알고리즘이 파이에서 도는지, 얼마나 걸리는지 확인용.

situation_viewer.pyw 에서 파일을 열었을 때와 같은 판정(situation_core.analyze)을 돌리고,
녹화마다 최종 판정(이상 행동 1순위, 정상 행동 2순위)과 걸린 시간을 찍는다.

    python judge_file.py recordings/                          # 폴더의 *_tracks_raw.csv 전부
    python judge_file.py recordings/3-5_tracks_raw.csv --detail   # 사람별로 자세히
    python judge_file.py recordings/ --labels recordings/labels_1111.csv   # 정답과 비교 (O / △ / X)
    python judge_file.py recordings/ --model behavior/models/situation_trees_real.pkl
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for cand in (HERE / "behavior", HERE.parent / "behavior", HERE.parent / "sg-radar-tools" / "tools" / "behavior"):  # 파이 / 저장소 안 / Capstone
    if (cand / "situation_core.py").exists():
        sys.path.insert(0, str(cand))
        BEHAVIOR = cand
        break
else:
    sys.exit("situation_core.py 를 못 찾음: ./behavior, ../behavior, ../sg-radar-tools/tools/behavior 중 한 곳에 있어야 함")

import situation_core as C  # noqa: E402


def pad(text: str, width: int) -> str:
    """한글은 두 칸으로 세서 왼쪽 정렬"""
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(width - w, 1)


def recording_name(p: Path) -> str:
    n = p.name
    for suf in ("_tracks_raw.csv", "_tracks.csv", ".csv", ".pcr"):
        if n.endswith(suf):
            return n[: -len(suf)]
    return p.stem


def read_labels(path: Path) -> dict[str, str]:
    with C.open_csv_text(path) as f:
        return {recording_name(Path(r["file"])): r["situation"].strip()
                for r in csv.DictReader(f) if r.get("file") and r.get("situation")}


def collect(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(p.glob("*_tracks_raw.csv"), key=lambda q: [(0, int(x)) if x.isdigit() else (1, x)
                                                                     for x in recording_name(q).split("-")])
        else:
            out.append(p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="녹화 파일을 판정한다 (파이에서 알고리즘 시험)")
    ap.add_argument("files", nargs="+", type=Path, help="트랙 CSV 파일 또는 폴더")
    ap.add_argument("--model", type=Path, default=BEHAVIOR / "models" / "situation_trees_real.pkl")
    ap.add_argument("--labels", type=Path, help="정답 표 CSV (file, situation 열)")
    ap.add_argument("--detail", action="store_true", help="사람별 판정과 근거도 찍기")
    a = ap.parse_args()

    t = time.perf_counter()
    models = C.load_models(a.model)
    print(f"모델 {a.model.name} (버전 {C.MODEL_VERSION}) 읽는 데 {time.perf_counter() - t:.2f}초")
    region = C.model_region(models)
    truth = read_labels(a.labels) if a.labels else {}
    files = collect(a.files)
    if not files:
        sys.exit("판정할 트랙 CSV가 없습니다")

    times, marks = [], {"O": 0, "△": 0, "X": 0}
    print("\n" + pad("녹화", 8) + pad("판정", 22) + " 확률  사람    시간" + ("  정답" if truth else ""))
    for p in files:
        name = recording_name(p)
        try:
            rows = C.load_rows(p)
        except Exception as exc:  # noqa: BLE001
            print(pad(name, 8) + f"읽기 실패: {exc}")
            continue
        t = time.perf_counter()
        tracks = C.analyze(C.tracks_from_rows(rows), models)
        spent = time.perf_counter() - t
        times.append(spent)
        labels = C.recording_labels(tracks)
        _, prob, _ = C.recording_verdict(tracks)
        people = [tr for tr in tracks if not getattr(tr, "out_of_range", False)]
        line = pad(name, 8) + pad(" + ".join(labels), 22) + f"{prob:>5.0%}  {len(people):>3}  {spent:>5.2f}s"
        if name in truth:
            m = C.match_labels(labels, truth[name])
            marks[m] += 1
            line += f"  {truth[name]} {m}"
        print(line)
        if a.detail:
            for tr in people:
                print("    " + pad(C.track_name(tr), 13) + pad(C.final_text(tr), 16) + f"{tr.final_prob:>4.0%}  "
                      f"{C.describe_path(tr, region)}")
                if getattr(tr, "rule_note", ""):
                    print(f"    {'':<12} 근거: {tr.rule_note}")
                for cam in tr.cameras:
                    print(f"    {'':<12} 촬영: {cam['situation']} {cam['prob']:.0%} "
                          f"({cam['t'] - tr.t_abs[0]:.1f}초)")

    if times:
        print(f"\n{len(times)}개 판정, 녹화 하나당 평균 {statistics.mean(times):.2f}초, 가장 긴 것 {max(times):.2f}초")
        print("  (실시간 서버는 최근 180초를 0.5초마다 판정하므로, 녹화 하나가 0.5초 안에 끝나면 실시간으로 충분)")
    if truth and sum(marks.values()):
        n = sum(marks.values())
        print(f"정답 비교 {n}개: O {marks['O']}  △ {marks['△']}  X {marks['X']}  "
              f"(O+△ {(marks['O'] + marks['△']) / n:.0%})")


if __name__ == "__main__":
    main()
