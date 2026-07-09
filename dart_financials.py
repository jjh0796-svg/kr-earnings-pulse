"""DART 정기보고서에서 분기 실적 시계열을 만들고 차트를 그린다.

분기 값 규칙 (fnlttSinglAcnt, 연결 CFS):
- 1분기(11013)/반기(11012)/3분기(11014)의 thstrm_amount = 해당 분기 3개월 값
- 4분기 = 사업보고서(11011) 연간 − 3분기보고서(11014) 누적
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import requests

LOGGER = logging.getLogger("dart_financials")

FIN_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
REPORT_CODES = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _parse_amount(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _fetch_report(api_key: str, corp_code: str, year: int, reprt_code: str) -> dict | None:
    resp = requests.get(
        FIN_URL,
        params={
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": reprt_code,
        },
        timeout=15,
    )
    data = resp.json()
    if data.get("status") != "000":
        return None
    out: dict = {}
    for item in data.get("list", []):
        if item.get("fs_div") != "CFS":
            continue
        name = item.get("account_nm")
        if name in ("매출액", "영업이익"):
            key = "rev" if name == "매출액" else "op"
            out[key] = _parse_amount(item.get("thstrm_amount"))
            out[f"{key}_cum"] = _parse_amount(item.get("thstrm_add_amount"))
    return out or None


def fetch_quarterly_series(api_key: str, corp_code: str, max_quarters: int = 11) -> list[dict]:
    """[(연도, 분기, 매출, 영업이익)] 최신순이 아닌 오래된 순으로 반환."""
    today = date.today()
    series: dict[tuple[int, int], dict] = {}
    yearly_cache: dict[tuple[int, str], dict | None] = {}

    def report(year: int, code: str):
        key = (year, code)
        if key not in yearly_cache:
            try:
                yearly_cache[key] = _fetch_report(api_key, corp_code, year, code)
            except Exception as exc:
                LOGGER.info("보고서 조회 실패(%s %s): %s", year, code, exc)
                yearly_cache[key] = None
        return yearly_cache[key]

    for year in range(today.year, today.year - 5, -1):
        for quarter in (4, 3, 2, 1):
            if len(series) >= max_quarters:
                break
            if quarter == 4:
                annual = report(year, REPORT_CODES[4])
                q3 = report(year, REPORT_CODES[3])
                if annual and q3 and annual.get("rev") and q3.get("rev_cum"):
                    rev = annual["rev"] - q3["rev_cum"]
                    op = (annual.get("op") or 0) - (q3.get("op_cum") or 0)
                    series[(year, 4)] = {"rev": rev, "op": op}
            else:
                rpt = report(year, REPORT_CODES[quarter])
                if rpt and rpt.get("rev"):
                    series[(year, quarter)] = {"rev": rpt["rev"], "op": rpt.get("op")}
        if len(series) >= max_quarters:
            break

    ordered = sorted(series.items())
    return [
        {"year": y, "quarter": q, "rev": v["rev"], "op": v.get("op")}
        for (y, q), v in ordered
    ]


def render_quarterly_chart(
    name: str,
    series: list[dict],
    output_path: Path,
    *,
    provisional: dict | None = None,
) -> Path | None:
    """매출 막대(최근 3개 레이블) + 영업이익률 선. provisional은 잠정치 추가분."""
    rows = list(series)
    if provisional:
        rows.append(provisional)
    if len(rows) < 6:
        LOGGER.info("차트 생략(%s): 분기 %d개뿐", name, len(rows))
        return None

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager, pyplot as plt

    available = {f.name for f in font_manager.fontManager.ttflist}
    for candidate in ("Malgun Gothic", "NanumGothic", "Noto Sans CJK KR"):
        if candidate in available:
            plt.rcParams["font.family"] = candidate
            plt.rcParams["axes.unicode_minus"] = False
            break

    labels = [f"{r['year'] % 100}Q{r['quarter']}" for r in rows]
    rev_cho = [r["rev"] / 1e12 for r in rows]  # 원 → 조원
    op_cho = [
        (r["op"] / 1e12) if r.get("op") is not None else None for r in rows
    ]
    opm = [
        (r["op"] / r["rev"] * 100) if r.get("op") is not None and r["rev"] else None
        for r in rows
    ]

    fig, ax = plt.subplots(figsize=(11, 5.2))
    colors = ["#8a8a8a"] * len(rows)
    colors[-1] = "#d62728"
    ax.bar(range(len(rows)), rev_cho, color=colors, width=0.72, label="매출")
    op_x = [i for i, v in enumerate(op_cho) if v is not None]
    ax.bar(
        op_x,
        [op_cho[i] for i in op_x],
        color="#ff8c00",
        edgecolor="white",
        linewidth=0.6,
        width=0.36,
        label="영업이익",
    )
    for idx in range(max(0, len(rows) - 3), len(rows)):
        ax.annotate(
            f"{rev_cho[idx]:,.1f}",
            xy=(idx, rev_cho[idx]),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            fontsize=10,
            fontweight="bold",
            color="#d62728" if idx == len(rows) - 1 else "#333333",
        )
        if op_cho[idx] is not None:
            ax.annotate(
                f"{op_cho[idx]:,.1f}",
                xy=(idx, op_cho[idx]),
                xytext=(0, 4),
                textcoords="offset points",
                ha="center",
                fontsize=9.5,
                fontweight="bold",
                color="#c05a00",
                bbox=dict(facecolor="white", alpha=0.85, edgecolor="none", pad=1.2),
            )
    suffix = " (최신=잠정치)" if provisional else ""
    ax.set_title(f"{name} 분기 매출·영업이익 (조원, 연결){suffix}", fontsize=13, fontweight="bold")
    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels(labels, fontsize=9)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    ax.legend(loc="upper left", fontsize=9, frameon=False)

    opm_points = [(i, v) for i, v in enumerate(opm) if v is not None]
    if len(opm_points) >= 4:
        ax2 = ax.twinx()
        ax2.plot(
            [p[0] for p in opm_points],
            [p[1] for p in opm_points],
            color="#1f77b4",
            marker="o",
            markersize=3.5,
            linewidth=1.6,
        )
        ax2.set_ylabel("영업이익률 %", color="#1f77b4", fontsize=9)

    for spine in ("top",):
        ax.spines[spine].set_visible(False)
    fig.text(0.99, 0.01, "데이터: DART 정기보고서 + 잠정공시", ha="right", fontsize=8, color="#666666")
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path
