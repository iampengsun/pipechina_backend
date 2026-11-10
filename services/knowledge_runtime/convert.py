"""Run untrusted conversions in a separate process with a hard parent deadline."""

import json
import sys
from pathlib import Path


def convert(path: Path, max_pages: int, *, full_page_ocr: bool = False) -> dict:
    if path.suffix in {".txt", ".md", ".csv"}:
        content = path.read_text(encoding="utf-8-sig")
        blocks = []
        lines = [content] if path.suffix == ".csv" else content.split("\n\n")
        for i, text in enumerate(lines):
            if text.strip():
                blocks.append(
                    {
                        "id": f"b{i}",
                        "kind": "table"
                        if path.suffix == ".csv"
                        else "heading"
                        if text.startswith("#") and "\n" not in text
                        else "paragraph",
                        "text": text,
                        "page": 1,
                        "locator": {"paragraph": i, "format": path.suffix},
                    }
                )
        return {"blocks": blocks, "warnings": [], "parser_version": "structured-text-v1"}

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import OcrMode, PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import TableItem

    options = PdfPipelineOptions(do_ocr=True, do_table_structure=True)
    options.ocr_options = RapidOcrOptions(backend="onnxruntime")
    if full_page_ocr:
        options.ocr_options.mode = OcrMode.FULL_PAGE
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    result = converter.convert(path, max_num_pages=max_pages, max_file_size=200 * 1024 * 1024)
    if str(result.status.value) != "success":
        raise ValueError("conversion incomplete; review required")
    document = result.document
    blocks, warnings = [], []
    for item, _ in document.iterate_items():
        label = str(item.label.value)
        if label in {"page_header", "page_footer"}:
            continue
        text = (
            item.export_to_markdown(doc=document)
            if isinstance(item, TableItem)
            else getattr(item, "text", "")
        )
        if not text.strip():
            if label == "picture":
                warnings.append(
                    f"Picture {item.self_ref}: no textual description; inspect original"
                )
            continue
        provenance = [p.model_dump(mode="json") for p in getattr(item, "prov", [])]
        blocks.append(
            {
                "id": item.self_ref,
                "parent_id": item.parent.cref if item.parent else None,
                "kind": "table"
                if isinstance(item, TableItem)
                else "heading"
                if label in {"title", "section_header"}
                else "list"
                if label == "list_item"
                else "paragraph",
                "text": text,
                "page": provenance[0]["page_no"] if provenance else 1,
                "locator": {"ref": item.self_ref, "provenance": provenance, "label": label},
            }
        )
    if not blocks and path.suffix.lower() == ".pdf" and not full_page_ocr:
        result = convert(path, max_pages, full_page_ocr=True)
        result["warnings"].append("full_page_ocr_fallback")
        return result
    if not blocks:
        raise ValueError("no usable text")
    return {"blocks": blocks, "warnings": warnings, "parser_version": "docling-structure-v1"}


if __name__ == "__main__":
    result = convert(Path(sys.argv[1]), int(sys.argv[2]))
    Path(sys.argv[3]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
