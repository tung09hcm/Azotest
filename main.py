"""
crop_pdf_api.py
---------------
FastAPI endpoint nhận file PDF, tự động phát hiện các section câu hỏi / bài tập
in đậm, cắt từng section thành ảnh PNG và trả về dạng base64 JSON.

Cài đặt:
    pip install fastapi uvicorn pymupdf pillow python-multipart

Chạy:
    uvicorn crop_pdf_api:app --reload

Gọi API:
    POST /crop-sections
    Content-Type: multipart/form-data
    Body: file=<pdf_file>, scale=2.0, keywords=cau,bai,question
"""

import base64
import io
import unicodedata
from typing import Optional

import fitz  # PyMuPDF
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image

app = FastAPI(title="PDF Section Cropper")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Hằng số
# ---------------------------------------------------------------------------
DEFAULT_SCALE = 2.0
DEFAULT_KEYWORDS = ["cau", "bai", "question", "questions", "task", "exercise"]
WHITESPACE_THRESHOLD = 250
WHITESPACE_MIN_CONTENT_PIXELS = 5


# ---------------------------------------------------------------------------
# Tiện ích
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def is_bold_span(span: dict) -> bool:
    return bool(span.get("flags", 0) & 16) or "Bold" in span.get("font", "")


def trim_whitespace(img: Image.Image) -> Image.Image:
    gray = img.convert("L")
    pixels = list(gray.getdata())
    width, height = gray.size

    def has_content(y: int) -> bool:
        row = pixels[y * width: (y + 1) * width]
        return sum(1 for p in row if p < WHITESPACE_THRESHOLD) >= WHITESPACE_MIN_CONTENT_PIXELS

    top = next((y for y in range(height) if has_content(y)), 0)
    bottom = next((y for y in range(height - 1, top, -1) if has_content(y)), height - 1)
    return img.crop((0, top, width, bottom + 1))


def pix_to_pil(pix: fitz.Pixmap) -> Image.Image:
    return Image.open(io.BytesIO(pix.tobytes("png")))


def image_to_base64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def stack_images(images: list[Image.Image]) -> Image.Image:
    images = [trim_whitespace(img) for img in images]
    total_w = max(img.width for img in images)
    total_h = sum(img.height for img in images)
    combined = Image.new("RGB", (total_w, total_h), (255, 255, 255))
    y = 0
    for img in images:
        combined.paste(img, (0, y))
        y += img.height
    return combined


# ---------------------------------------------------------------------------
# Phát hiện sections
# ---------------------------------------------------------------------------
def detect_sections(doc: fitz.Document, keywords: list[str]) -> list[dict]:
    """Trả về list [{"text", "page_num", "y_top"}]"""
    buckets: dict[str, list[dict]] = {}

    for page_idx, page in enumerate(doc):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if not is_bold_span(span):
                        continue
                    norm = normalize_text(span["text"])
                    prefix = norm[:3]
                    x0 = span["bbox"][0]

                    if not buckets:
                        if any(kw in norm for kw in keywords):
                            buckets[prefix] = [{"text": span["text"].strip(),
                                                "page_num": page_idx,
                                                "bbox": span["bbox"]}]
                    else:
                        if prefix in buckets:
                            if x0 == buckets[prefix][0]["bbox"][0]:
                                buckets[prefix].append({"text": span["text"].strip(),
                                                        "page_num": page_idx,
                                                        "bbox": span["bbox"]})
                        else:
                            buckets[prefix] = [{"text": span["text"].strip(),
                                                "page_num": page_idx,
                                                "bbox": span["bbox"]}]

    if not buckets:
        return []

    best = max(buckets, key=lambda k: len(buckets[k]))
    spans = sorted(buckets[best], key=lambda s: (s["page_num"], s["bbox"][1]))
    return [{"text": s["text"], "page_num": s["page_num"], "y_top": s["bbox"][1]} for s in spans]


# ---------------------------------------------------------------------------
# Render section → ảnh
# ---------------------------------------------------------------------------
def render_section(
    doc: fitz.Document,
    mat: fitz.Matrix,
    start: dict,
    end: Optional[dict],
) -> Image.Image:
    def clip(page_idx: int, y0: float, y1: Optional[float] = None) -> fitz.Pixmap:
        page = doc[page_idx]
        y_bottom = y1 if y1 is not None else page.rect.height
        return page.get_pixmap(matrix=mat, clip=fitz.Rect(0, y0, page.rect.width, y_bottom))

    if end is None:
        pixmaps = [clip(p, start["y_top"] if p == start["page_num"] else 0.0)
                   for p in range(start["page_num"], doc.page_count)]
    elif start["page_num"] == end["page_num"]:
        pixmaps = [clip(start["page_num"], start["y_top"], end["y_top"])]
    else:
        pixmaps = [clip(start["page_num"], start["y_top"])]
        for p in range(start["page_num"] + 1, end["page_num"]):
            pixmaps.append(clip(p, 0.0))
        pixmaps.append(clip(end["page_num"], 0.0, end["y_top"]))

    images = [pix_to_pil(pix) for pix in pixmaps]
    return stack_images(images) if len(images) > 1 else trim_whitespace(images[0])


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@app.post("/crop-sections")
async def crop_sections(
    file: UploadFile = File(..., description="File PDF cần xử lý"),
    scale: float = Form(DEFAULT_SCALE, description="Hệ số phóng to (1.0=72dpi, 2.0=144dpi)"),
    keywords: str = Form(
        ",".join(DEFAULT_KEYWORDS),
        description="Từ khoá nhận diện tiêu đề, cách nhau bằng dấu phẩy",
    ),
):
    """
    Nhận file PDF, cắt từng section câu hỏi/bài tập thành ảnh PNG.

    Trả về JSON:
    ```json
    {
      "total": 5,
      "sections": [
        {
          "index": 1,
          "title": "Câu 1",
          "page": 1,
          "image_base64": "iVBORw0KGgo..."
        },
        ...
      ]
    }
    ```
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Chỉ chấp nhận file PDF.")

    kw_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]
    pdf_bytes = await file.read()

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Không đọc được PDF: {e}")

    spans = detect_sections(doc, kw_list)
    if not spans:
        doc.close()
        raise HTTPException(
            status_code=404,
            detail=f"Không tìm thấy section nào với từ khoá: {kw_list}",
        )

    mat = fitz.Matrix(scale, scale)
    results = []

    for i, span in enumerate(spans):
        end_span = spans[i + 1] if i + 1 < len(spans) else None
        img = render_section(doc, mat, span, end_span)
        results.append({
            "index": i + 1,
            "title": span["text"],
            "page": span["page_num"] + 1,
            "image_base64": image_to_base64(img),
        })

    doc.close()
    return JSONResponse({"total": len(results), "sections": results})