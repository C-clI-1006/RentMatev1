"""Render the checked-in Chinese team guide. Optional build dependency: reportlab.

Usage: python docs/build_team_demo_pdf.py [--font /path/to/chinese-font.ttf]
The resulting PDF is an artifact; no runtime dependency is added to RentMate.
"""
from pathlib import Path
import argparse
import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle, Preformatted,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/全组全链路演示与测试说明.md"
OUTPUT = ROOT / "output/pdf/RentMate_全组全链路演示与测试说明_20261030.pdf"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", default="/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    args = parser.parse_args()
    pdfmetrics.registerFont(TTFont("GuideCJK", args.font))
    styles = getSampleStyleSheet()
    base = dict(fontName="GuideCJK", wordWrap="CJK", textColor=colors.HexColor("#243d43"), alignment=TA_LEFT)
    styles.add(ParagraphStyle("GuideBody", fontSize=9.4, leading=15.5, spaceAfter=9, **base))
    styles.add(ParagraphStyle("GuideTitle", fontSize=23, leading=32, spaceAfter=19, **base))
    styles.add(ParagraphStyle("GuideH2", fontSize=15.5, leading=23, spaceAfter=12, spaceBefore=8, keepWithNext=True, **base))
    styles.add(ParagraphStyle("GuideH3", fontSize=11.5, leading=18, spaceAfter=8, spaceBefore=9, keepWithNext=True, **base))
    styles.add(ParagraphStyle("GuideCell", fontSize=8.1, leading=12.6, **base))
    styles.add(ParagraphStyle("GuideCode", fontName="Courier", fontSize=8, leading=12,
                              backColor=colors.HexColor("#eef3f0"), borderPadding=10,
                              spaceBefore=5, spaceAfter=12))
    width = A4[0] - 88
    story = []
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1; continue
        if line == "<!-- page -->":
            story.append(PageBreak()); i += 1; continue
        if line.startswith("```"):
            code = []; i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i]); i += 1
            # Chinese comments need the CJK font, while commands remain monospace.
            if any(re.search(r"[\u4e00-\u9fff]", text) for text in code):
                story.append(Paragraph("<br/>".join(escape(text) for text in code), styles["GuideBody"]))
            else:
                story.append(Preformatted("\n".join(code), styles["GuideCode"]))
            i += 1; continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = [cell.strip() for cell in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r"[-: ]+", cell) for cell in row):
                    rows.append([Paragraph(escape(cell), styles["GuideCell"]) for cell in row])
                i += 1
            n = len(rows[0])
            fractions = {2:[.30,.70],3:[.24,.38,.38]}.get(n,[1/n]*n)
            table = Table(rows, colWidths=[width*f for f in fractions], repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#e2eee7")),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#f6f8f5")]),
                ("VALIGN",(0,0),(-1,-1),"TOP"),
                ("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),
                ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
                ("LINEBELOW",(0,0),(-1,0),.6,colors.HexColor("#bdd1c5")),
            ]))
            story.extend([table,Spacer(1,12)]); continue
        if line.startswith("# "):
            story.append(Paragraph(escape(line[2:]),styles["GuideTitle"]))
        elif line.startswith("## "):
            story.append(Paragraph(escape(line[3:]),styles["GuideH2"]))
        elif line.startswith("### "):
            story.append(Paragraph(escape(line[4:]),styles["GuideH3"]))
        else:
            story.append(Paragraph(escape(line),styles["GuideBody"]))
        i += 1

    def page_frame(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#ccdcd3")); canvas.setLineWidth(.6)
        canvas.line(44,A4[1]-38,A4[0]-44,A4[1]-38)
        canvas.setFont("GuideCJK",8); canvas.setFillColor(colors.HexColor("#5d7670"))
        canvas.drawString(44,A4[1]-29,"RENTMATE  /  STAGE 2  /  全组测试手册")
        canvas.drawString(44,26,"2026-10-30  |  本地演示与验收，非生产部署承诺")
        canvas.drawRightString(A4[0]-44,26,f"第 {doc.page} 页")
        canvas.restoreState()

    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    document = SimpleDocTemplate(str(OUTPUT),pagesize=A4,leftMargin=44,rightMargin=44,
                                topMargin=52,bottomMargin=46,title="RentMate 全组全链路演示与测试说明",
                                author="RentMate Stage 2 Team")
    document.build(story,onFirstPage=page_frame,onLaterPages=page_frame)
    print(OUTPUT)


if __name__ == "__main__":
    main()
