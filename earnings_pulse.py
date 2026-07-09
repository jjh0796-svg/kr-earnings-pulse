"""국내 주요 종목 실적 공시(잠정/영업실적) 즉시 알림.

DART 당일 공시 목록에서 워치리스트 종목의 영업(잠정)실적류 공시를 감지해
텔레그램으로 보낸다. 공시 원문에서 매출액·영업이익을 best-effort로 파싱하되,
실패하면 링크만이라도 즉시 발송한다.

    python earnings_pulse.py --dry-run
    python earnings_pulse.py
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

LOGGER = logging.getLogger("earnings_pulse")

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DOC_URL = "https://opendart.fss.or.kr/api/document.xml"
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

REPORT_PATTERN = re.compile(r"영업\s*\(?잠정\)?\s*실적|매출액\s*또는\s*손익구조")


def load_watchlist() -> dict[str, str]:
    data = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    return {c["corp_code"]: c["name"] for c in data["companies"]}


def default_state_path() -> Path:
    return Path(os.environ.get("PULSE_STATE_FILE", ".pulse/state.json"))


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"sent": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        state.setdefault("sent", [])
        return state
    except (OSError, ValueError):
        return {"sent": []}


def save_state(path: Path, state: dict) -> None:
    state["sent"] = state.get("sent", [])[-300:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _api_key() -> str:
    key = (os.environ.get("DART_API_KEY") or "").strip()
    if not key:
        raise SystemExit("DART_API_KEY가 설정되지 않았습니다.")
    return key


def fetch_today_filings(api_key: str, today: str, watchlist: dict[str, str]) -> list[dict]:
    """워치리스트 종목별로 당일 공시를 직접 조회한다.
    (일자 전체 조회는 하루 수천 건이라 페이지 제한에 걸릴 수 있음)"""
    filings: list[dict] = []
    for corp_code in watchlist:
        try:
            resp = requests.get(
                LIST_URL,
                params={
                    "crtfc_key": api_key,
                    "corp_code": corp_code,
                    "bgn_de": today,
                    "end_de": today,
                    "page_count": "100",
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("status") not in ("000", "013"):  # 013 = 조회 결과 없음
                LOGGER.warning("DART 응답 상태(%s): %s %s", corp_code, data.get("status"), data.get("message"))
                continue
            filings.extend(data.get("list", []))
        except Exception as exc:
            LOGGER.warning("DART 조회 실패(%s): %s", corp_code, exc)
    return filings


def _extract_document_text(api_key: str, rcept_no: str) -> str:
    resp = requests.get(
        DOC_URL, params={"crtfc_key": api_key, "rcept_no": rcept_no}, timeout=30
    )
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    texts = []
    for name in zf.namelist():
        raw = zf.read(name)
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                texts.append(raw.decode(enc))
                break
            except UnicodeDecodeError:
                continue
    text = " ".join(texts)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def _fmt_krw(value: float, unit_won: float) -> str:
    """백만원/천원 등 단위를 곱해 조/억 단위 한글 표기로."""
    won = value * unit_won
    if abs(won) >= 1e12:
        return f"{won / 1e12:,.1f}조원"
    return f"{won / 1e8:,.0f}억원"


def _detect_unit_won(text: str) -> float:
    if re.search(r"단위\s*[:：]?\s*조\s*원", text):
        return 1e12
    if re.search(r"단위\s*[:：]?\s*억\s*원", text):
        return 1e8
    if re.search(r"단위\s*[:：]?\s*천\s*원", text):
        return 1e3
    return 1e6  # 공정공시 기본은 백만원


def parse_earnings_numbers(text: str) -> dict | None:
    """잠정실적 공정공시 표에서 매출액/영업이익을 추출.
    표 구조: 라벨 당해실적 [당해, 직전분기, 직전대비%, 전년동기, 전년대비%] 누계실적 ..."""
    unit_won = _detect_unit_won(text)

    def find_metric(label: str) -> tuple[float, float | None] | None:
        match = re.search(
            re.escape(label) + r"\s*당해실적((?:\s*(?:-|△?[\d,]+(?:\.\d+)?)){3,9})",
            text,
        )
        if not match:
            return None
        tokens = re.findall(r"△?[\d,]+(?:\.\d+)?", match.group(1))
        numbers = []
        for token in tokens:
            try:
                numbers.append(float(token.replace(",", "").replace("△", "-")))
            except ValueError:
                return None
        if not numbers:
            return None
        current = numbers[0]
        yoy_pct = numbers[-1] if len(numbers) >= 3 and abs(numbers[-1]) < 5000 else None
        return current, yoy_pct

    revenue = find_metric("매출액")
    op = find_metric("영업이익")
    if not revenue or not op:
        return None
    if revenue[0] <= 0 or abs(op[0]) > revenue[0] * 3:
        return None  # 파싱이 이상하면 숫자 없이 발송
    return {
        "revenue": _fmt_krw(revenue[0], unit_won),
        "revenue_pct": revenue[1],
        "op": _fmt_krw(op[0], unit_won),
        "op_pct": op[1],
        "revenue_won": revenue[0] * unit_won,
        "op_won": op[0] * unit_won,
    }


def infer_quarter(doc_text: str, rcept_dt: str) -> tuple[int, int]:
    """공시 원문 또는 접수월로 대상 분기를 추정한다."""
    match = re.search(r"(20\d{2})\s*년\s*(?:제?\s*)?([1-4])\s*분기", doc_text)
    if match:
        return int(match.group(1)), int(match.group(2))
    year, month = int(rcept_dt[:4]), int(rcept_dt[4:6])
    quarter_by_month = {1: 4, 2: 4, 3: 4, 4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3}
    quarter = quarter_by_month[month]
    if quarter == 4 and month <= 3:
        year -= 1
    return year, quarter


def build_message(
    name: str,
    report_nm: str,
    rcept_no: str,
    numbers: dict | None,
    rcept_dt: str = "",
    quarter: tuple[int, int] | None = None,
) -> str:
    lines = [f"🚨 <b>[실적 공시] {name}</b>", report_nm.strip()]
    meta = []
    if quarter:
        meta.append(f"대상: {quarter[0]}년 {quarter[1]}분기")
    if rcept_dt:
        meta.append(f"공시일 {rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}")
    if meta:
        lines.append(" · ".join(meta))
    if numbers:
        rev_pct = f" (전년동기 {numbers['revenue_pct']:+.1f}%)" if numbers.get("revenue_pct") is not None else ""
        op_pct = f" (전년동기 {numbers['op_pct']:+.1f}%)" if numbers.get("op_pct") is not None else ""
        lines.append(f"매출액 {numbers['revenue']}{rev_pct}")
        lines.append(f"영업이익 {numbers['op']}{op_pct}")
        lines.append("* 공시 원문 자동추출 값 — 원문 확인 권장")
    else:
        lines.append("숫자 자동추출 실패 — 원문에서 확인해 주세요")
    lines.append(VIEWER_URL.format(rcept_no=rcept_no))
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID가 설정되지 않았습니다.")
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=30,
    ).raise_for_status()


def send_telegram_photo(photo_path: Path, caption: str) -> None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    with photo_path.open("rb") as handle:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": handle},
            timeout=60,
        ).raise_for_status()


def run_once(*, dry_run: bool, state_path: Path | None = None, today: str | None = None, force: bool = False) -> int:
    state_path = state_path or default_state_path()
    state = load_state(state_path)
    sent = set(state["sent"])
    api_key = _api_key()
    today = today or datetime.now(KST).strftime("%Y%m%d")
    watchlist = load_watchlist()

    hits = 0
    for filing in fetch_today_filings(api_key, today, watchlist):
        corp_code = filing.get("corp_code", "")
        report_nm = filing.get("report_nm", "")
        rcept_no = filing.get("rcept_no", "")
        if corp_code not in watchlist or not REPORT_PATTERN.search(report_nm):
            continue
        if rcept_no in sent and not force:
            continue
        numbers = None
        doc_text = ""
        try:
            doc_text = _extract_document_text(api_key, rcept_no)
            numbers = parse_earnings_numbers(doc_text)
        except Exception as exc:
            LOGGER.warning("공시 원문 파싱 실패(%s): %s", rcept_no, exc)
        rcept_dt = filing.get("rcept_dt", "")
        quarter = infer_quarter(doc_text, rcept_dt) if rcept_dt else None
        message = build_message(watchlist[corp_code], report_nm, rcept_no, numbers, rcept_dt, quarter)
        chart_path = None
        if numbers and quarter:
            try:
                from dart_financials import fetch_quarterly_series, render_quarterly_chart

                history = fetch_quarterly_series(api_key, corp_code, max_quarters=11)
                history = [h for h in history if (h["year"], h["quarter"]) != quarter]
                provisional = {"year": quarter[0], "quarter": quarter[1],
                               "rev": numbers["revenue_won"], "op": numbers["op_won"]}
                chart_path = render_quarterly_chart(
                    watchlist[corp_code], history, Path(f".pulse/{corp_code}_chart.png"),
                    provisional=provisional,
                )
            except Exception as exc:
                LOGGER.warning("분기 차트 생성 실패(%s): %s", corp_code, exc)
        if dry_run:
            print("=" * 50)
            print(message)
            if chart_path:
                print(f"[차트 저장됨: {chart_path}]")
        else:
            send_telegram(message)
            if chart_path:
                send_telegram_photo(chart_path, f"{watchlist[corp_code]} 최근 12분기 매출·영업이익률")
            if rcept_no not in state["sent"]:
                state["sent"].append(rcept_no)
            save_state(state_path, state)
            LOGGER.info("실적 공시 알림 발송: %s %s", watchlist[corp_code], rcept_no)
        hits += 1
    if hits == 0:
        LOGGER.info("신규 실적 공시 없음 (%s).", today)
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description="국내 주요 종목 실적 공시 즉시 알림.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", help="YYYYMMDD (테스트용)")
    parser.add_argument("--force", action="store_true", help="이미 보낸 공시도 다시 발송")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    run_once(dry_run=args.dry_run, today=args.date, force=args.force)


if __name__ == "__main__":
    main()
