"""Build deterministic synthetic OCR/table/image fixtures; no expert or plant data."""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas


def generate(root):
    root.mkdir(parents=True, exist_ok=True)
    font_path = next(
        (
            p
            for p in [
                Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            ]
            if p.exists()
        ),
        None,
    )
    font = ImageFont.truetype(str(font_path), 40) if font_path else ImageFont.load_default(size=40)
    image = Image.new("RGB", (1400, 900), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(
        [
            "SYNTHETIC ACCEPTANCE",
            "OCR-MARKER-7429",
            "No field operation is authorized.",
            "Valve DEMO-A has label LABEL-A.",
        ]
    ):
        draw.text((80, 120 + index * 120), line, font=font, fill="black")
    image.save(root / "synthetic.png")
    pdf = Canvas(str(root / "scanned.pdf"), pagesize=(700, 450))
    pdf.drawImage(ImageReader(image), 0, 0, width=700, height=450)
    pdf.save()
    pdf = Canvas(str(root / "cross-page-table.pdf"), pagesize=(595, 842))
    for page, marker in enumerate(["TABLE-PAGE-A-7429", "TABLE-PAGE-B-8530"], 1):
        pdf.setFont("Helvetica", 18)
        pdf.drawString(40, 790, "SYNTHETIC ACCEPTANCE TABLE")
        pdf.drawString(40, 752, marker)
        for index, label in enumerate(["DEMO ID", "READING", "UNIT"]):
            pdf.drawString(45 + index * 170, 700, label)
        for index in range(10):
            y = 660 - index * 45
            pdf.setFont("Helvetica", 12)
            pdf.drawString(45, y, f"DEMO-{page}-{index}")
            pdf.drawString(215, y, str(index + page))
            pdf.drawString(385, y, "test-unit")
            pdf.line(40, y - 8, 550, y - 8)
        for x in [40, 205, 375, 550]:
            pdf.line(x, 710, x, 235)
        pdf.showPage()
    pdf.save()
    manifest = {
        "provenance": "synthetic_acceptance",
        "expert_validated": False,
        "cases": [
            {
                "case_id": "scanned_ocr",
                "file": "scanned.pdf",
                "mime_type": "application/pdf",
                "expected_terms": ["OCR-MARKER-7429", "LABEL-A"],
            },
            {
                "case_id": "cross_page_table",
                "file": "cross-page-table.pdf",
                "mime_type": "application/pdf",
                "expected_terms": [
                    "TABLE-PAGE-A-7429",
                    "TABLE-PAGE-B-8530",
                    "DEMO-1-9",
                    "DEMO-2-9",
                ],
            },
        ],
    }
    (root / "documents.json").write_text(json.dumps(manifest, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    generate(p.parse_args().directory)


if __name__ == "__main__":
    main()
