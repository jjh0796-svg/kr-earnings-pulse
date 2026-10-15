"""국내판 '약속 대 실제' (2026-10-16).

국내 상장사는 미국처럼 분기 가이던스를 숫자로 잘 내지 않는다. 그래서 '시장의 약속' = 증권사 컨센서스를 기준으로
  ① 이번 분기 실적이 컨센서스 대비 얼마나 벗어났는지(매출·영업이익)
  ② 최근 4분기 연속 기록(상회/하회 — 서프라이즈 습관)
  ③ 연간 컨센서스 달성률(누계 실적 ÷ 연간 예상)
을 실적 속보에 붙이고, 회사가 숫자 가이던스를 낸 경우(guidance_kr.json 수기)엔 그것도 대조한다.

재료: 네이버 금융 모바일 API(m.stock.naver.com /api/stock/{code}/finance/quarter · annual, 단위 억원, isConsensus=Y 열이
컨센서스). kr-stock-watch summarize._naver_finance와 같은 읽기 방식. 실패하면 조용히 빈 값 — 속보 자체는 그대로 나간다.
이력은 state["track"][stock_code] = [{"q": "2026Q3", "rev": 억, "op": 억, "rev_cons": 억, "op_cons": 억}, ...] (최신 뒤).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import requests

LOGGER = logging.getLogger("kr_track")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"}
GUIDANCE_FILE = Path(__file__).resolve().parent / "guidance_kr.json"


def _num(value) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def naver_finance(code: str, period: str) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    """(실적 열들, 컨센서스 열들) — 각 (키, {rev, op, ni}) 억원. 키는 '2026.09'(분기) / '2026.12'(연간) 꼴."""
    r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/finance/{period}", headers=UA, timeout=10)
    r.raise_for_status()
    info = r.json().get("financeInfo", {})
    cols = [(t.get("key"), t.get("isConsensus") == "Y") for t in info.get("trTitleList", [])]
    rows = {row.get("title"): row.get("columns", {}) for row in info.get("rowList", [])
            if row.get("title") in ("매출액", "영업이익", "당기순이익")}

    def cell(title, key):
        return _num((rows.get(title, {}).get(key) or {}).get("value"))

    past, cons = [], []
    for key, is_cons in cols:
        if not key:
            continue
        vals = {"rev": cell("매출액", key), "op": cell("영업이익", key), "ni": cell("당기순이익", key)}
        (cons if is_cons else past).append((key, vals))
    return past, cons


def qkey(year: int, quarter: int) -> str:
    return f"{year}Q{quarter}"


def naver_key_to_q(key: str) -> str | None:
    """'202609' / '2026.09' → '2026Q3' (네이버 trTitleList.key는 '202609' 꼴, title은 '2026.09.')."""
    m = re.fullmatch(r"(20\d\d)\.?(\d\d)\.?", key or "")
    if not m:
        return None
    return qkey(int(m[1]), (int(m[2]) - 1) // 3 + 1)


def load_guidance() -> dict:
    try:
        return json.loads(GUIDANCE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _pct(actual: float | None, expected: float | None) -> float | None:
    if actual is None or not expected:
        return None
    return (actual / expected - 1) * 100


def _eok(v: float | None) -> str:
    if v is None:
        return "-"
    if abs(v) >= 10000:
        return f"{v / 10000:,.1f}조"
    return f"{v:,.0f}억"


def record(state: dict, stock_code: str, quarter: tuple[int, int], rev_won: float, op_won: float,
           code_for_naver: str | None = None) -> dict | None:
    """실적 공시 1건을 컨센서스와 대조해 state에 기록하고, 메시지용 요약을 돌려준다. 네이버 실패 시 None."""
    code = code_for_naver or stock_code
    try:
        past_q, cons_q = naver_finance(code, "quarter")
        past_y, cons_y = naver_finance(code, "annual")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("네이버 컨센서스 조회 실패(%s): %s", code, exc)
        return None
    q = qkey(*quarter)
    rev, op = rev_won / 1e8, op_won / 1e8
    cons_this = next((v for k, v in cons_q if naver_key_to_q(k) == q), None)
    entry = {"q": q, "rev": round(rev, 1), "op": round(op, 1),
             "rev_cons": cons_this["rev"] if cons_this else None, "op_cons": cons_this["op"] if cons_this else None}
    hist = state.setdefault("track", {}).setdefault(stock_code, [])
    hist[:] = [h for h in hist if h.get("q") != q] + [entry]
    hist.sort(key=lambda h: h["q"])
    del hist[:-8]

    # 과거 분기도 네이버 실적 열로 채워 둔다(첫 실행부터 4분기 기록이 보이도록; 컨센서스는 그 당시 값이 아니라 빈칸)
    known = {h["q"] for h in hist}
    for k, v in past_q:
        qq = naver_key_to_q(k)
        if qq and qq < q and qq not in known and v.get("rev") is not None:
            hist.append({"q": qq, "rev": v["rev"], "op": v["op"], "rev_cons": None, "op_cons": None, "backfill": True})
    hist.sort(key=lambda h: h["q"])
    del hist[:-8]

    # 연간 달성률: 올해 누계(네이버 분기 실적 + 이번 분기) ÷ 연간 컨센서스
    year = quarter[0]
    ytd_rev = rev + sum(v["rev"] or 0 for k, v in past_q if (naver_key_to_q(k) or "").startswith(str(year)) and naver_key_to_q(k) < q)
    ytd_op = op + sum(v["op"] or 0 for k, v in past_q if (naver_key_to_q(k) or "").startswith(str(year)) and naver_key_to_q(k) < q)
    fy = next((v for k, v in cons_y if k.startswith(str(year))), None)
    guidance = load_guidance().get(stock_code, {}).get(str(year))
    return {"q": q, "entry": entry, "hist": hist, "ytd_rev": ytd_rev, "ytd_op": ytd_op, "fy_cons": fy,
            "quarter": quarter, "guidance": guidance}


def lines(summary: dict | None) -> list[str]:
    """실적 속보에 붙일 '컨센서스 대비' 블록."""
    if not summary:
        return []
    e = summary["entry"]
    out = []
    parts = []
    for label, a, c in (("매출", e["rev"], e["rev_cons"]), ("영업익", e["op"], e["op_cons"])):
        d = _pct(a, c)
        if d is not None:
            parts.append(f"{label} {_eok(c)} 예상 대비 {d:+.1f}%")
    if parts:
        out.append("🎯 컨센서스: " + " · ".join(parts))
    # 최근 4분기 서프라이즈 기록(컨센서스가 있던 분기만)
    marks = []
    for h in summary["hist"][-5:]:
        d = _pct(h.get("op"), h.get("op_cons"))
        if d is None:
            continue
        marks.append(f"{h['q'][2:]} {'▲' if d >= 0 else '▽'}{abs(d):.0f}%")
    if len(marks) >= 2:
        out.append("  영업익 기록: " + " · ".join(marks))
    fy, (year, quarter) = summary.get("fy_cons"), summary["quarter"]
    if fy and fy.get("op"):
        prog = summary["ytd_op"] / fy["op"] * 100
        out.append(f"📅 {year}년 연간 예상 영업익 {_eok(fy['op'])} 중 {quarter}분기까지 {prog:.0f}% 달성"
                   + (f" (매출 {summary['ytd_rev'] / fy['rev'] * 100:.0f}%)" if fy.get("rev") else ""))
    g = summary.get("guidance")
    if g:
        gl = []
        for label, key, ytd in (("매출", "rev", summary["ytd_rev"]), ("영업익", "op", summary["ytd_op"])):
            if g.get(key):
                gl.append(f"{label} {_eok(g[key])} 중 {ytd / g[key] * 100:.0f}%")
        if gl:
            out.append(f"🏢 회사 가이던스({g.get('source', '수기')}): " + " · ".join(gl))
    return out
