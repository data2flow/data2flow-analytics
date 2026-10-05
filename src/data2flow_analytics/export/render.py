"""차트 PNG(matplotlib, 1600×900)와 PDF 보고서(reportlab). 한글은 reportlab 내장 CID 글꼴(HYGothic-Medium)로 쓴다."""

from __future__ import annotations

import io
import logging
import warnings
from datetime import datetime

from ..clock import iso

log = logging.getLogger("data2flow_analytics.export")
PNG_SIZE = (1600, 900)


def chart_png(chart: dict) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import pandas as pd

    fig = plt.figure(figsize=(PNG_SIZE[0] / 100, PNG_SIZE[1] / 100), dpi=100)
    ax = fig.add_subplot(111)
    kind = chart.get("type")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if kind == "heatmap" and chart.get("heatmap"):
            hm = chart["heatmap"]
            values = [[float("nan") if v is None else v for v in row] for row in hm["values"]]
            im = ax.imshow(values, aspect="auto", cmap="viridis")
            ax.set_xticks(range(len(hm["xLabels"])), hm["xLabels"])
            ax.set_yticks(range(len(hm["yLabels"])), hm["yLabels"])
            fig.colorbar(im, ax=ax)
        elif kind == "gauge" and chart.get("gauge"):
            g = chart["gauge"]
            ax.barh([0], [g.get("value") or 0], color="#2f7ed8")
            ax.set_xlim(g.get("min", 0), g.get("max", 100))
        else:
            time_axis = (chart.get("xAxis") or {}).get("type") == "time"
            for s in chart.get("series") or []:
                pts = [p for p in s.get("data") or [] if p[1] is not None]
                if not pts:
                    continue
                xs = [pd.Timestamp(p[0]) for p in pts] if time_axis else [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if kind == "bar":
                    ax.bar([str(x) for x in xs], ys, label=s.get("label"))
                elif kind == "scatter":
                    ax.scatter(xs, ys, s=4, label=s.get("label"))
                else:
                    ax.plot(xs, ys, linestyle="--" if s.get("style") == "dashed" else "-", label=s.get("label"))
            for band in chart.get("bands") or []:
                if band.get("x") and time_axis:
                    xs = [pd.Timestamp(x) for x in band["x"]]
                    ax.fill_between(xs, band["lower"], band["upper"], alpha=0.2)
            for t in chart.get("thresholds") or []:
                ax.axhline(t["value"], color="#c0392b", linestyle=":")
            if time_axis:
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
            if chart.get("series"):
                ax.legend(loc="upper left", fontsize=8)
        ax.set_title(chart.get("title", ""))
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    return buf.getvalue()


def report_pdf(view: dict, generated_at: datetime) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    font = "HYGothic-Medium"
    try:
        pdfmetrics.getFont(font)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    result = view["result"]
    prov = result.get("provenance") or {}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    def footer() -> None:
        c.setFont(font, 8)
        period = prov.get("period") or {}
        c.drawString(15 * mm, 10 * mm, f"템플릿 {prov.get('template', '')} · 기간 {period.get('from', '')} ~ {period.get('to', '')} · "
                                       f"생성 {iso(generated_at)}")

    y = height - 20 * mm
    c.setFont(font, 14)
    c.drawString(15 * mm, y, result["summary"]["headline"][:80])
    y -= 10 * mm
    c.setFont(font, 9)
    for m in result["summary"].get("metrics", [])[:12]:
        c.drawString(18 * mm, y, f"{m['label']}: {m.get('value')} {m.get('unit') or ''}")
        y -= 5 * mm
    y -= 3 * mm
    c.drawString(15 * mm, y, f"데이터 출처: 바인딩 {len(prov.get('bindings') or [])}개, 포인트 {prov.get('points')}, "
                             f"누락률 {prov.get('missingRate')}, 품질 {prov.get('qualityFilter')}, 가상 포함 {prov.get('virtual')}")
    y -= 5 * mm
    if result.get("aiCommentaryId"):
        c.drawString(15 * mm, y, f"AI 해설: {result['aiCommentaryId']}")
        y -= 5 * mm
    for caveat in result.get("caveats", [])[:4]:
        c.drawString(15 * mm, y, f"※ {caveat[:90]}")
        y -= 5 * mm
    for chart in (result.get("charts") or [])[:3]:
        img_h = 70 * mm
        if y - img_h < 20 * mm:
            footer()
            c.showPage()
            y = height - 20 * mm
        png = chart_png(chart)
        c.drawImage(ImageReader(io.BytesIO(png)), 15 * mm, y - img_h, width=width - 30 * mm, height=img_h, preserveAspectRatio=True)
        y -= img_h + 5 * mm
    footer()
    c.showPage()
    c.save()
    return buf.getvalue()
