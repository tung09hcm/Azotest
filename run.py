"""
crop_pdf.py
===========
Tự động phát hiện các tiêu đề câu hỏi / bài tập in đậm trong file PDF,
sau đó cắt mỗi section thành một ảnh PNG riêng biệt.

Tính năng
---------
- Phát hiện span in đậm dựa trên font flag và tên font.
- Hỗ trợ section trải dài qua nhiều trang liên tiếp (cross-page stitching).
- Tự động loại bỏ khoảng trắng thừa ở đầu / cuối mỗi ảnh ghép.
- Scale độ phân giải đầu ra có thể cấu hình.

Yêu cầu
-------
    pip install pymupdf pillow

Sử dụng
-------
    python crop_pdf.py                      # dùng cấu hình mặc định
    python crop_pdf.py --input exam.pdf     # chỉ định file PDF
    python crop_pdf.py --scale 3.0          # tăng độ phân giải
    python crop_pdf.py --verbose            # bật chế độ debug
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import fitz  # PyMuPDF
from PIL import Image

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cấu hình mặc định
# ---------------------------------------------------------------------------
DEFAULT_PDF_PATH   = "file.pdf"
DEFAULT_OUTPUT_DIR = "output_images"
DEFAULT_SCALE      = 2.0

# Từ khoá nhận diện tiêu đề câu hỏi / bài tập (sau khi đã normalise)
SECTION_KEYWORDS: list[str] = [
    "cau", "bai", "question", "questions", "task", "exercise",
]

# Trim whitespace: pixel grayscale > ngưỡng này bị coi là "trắng"
WHITESPACE_THRESHOLD: int = 250   # 0–255
# Số pixel không-trắng tối thiểu trong một hàng để hàng đó được giữ lại
WHITESPACE_MIN_CONTENT_PIXELS: int = 5


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class SectionSpan:
    """Đại diện cho một tiêu đề section được phát hiện trong PDF."""

    text:     str
    page_num: int
    bbox:     tuple[float, float, float, float]  # (x0, y0, x1, y1)

    @property
    def y_top(self) -> float:
        """Toạ độ y trên cùng của span (PDF coordinate space)."""
        return self.bbox[1]

    @property
    def x_left(self) -> float:
        """Toạ độ x trái của span."""
        return self.bbox[0]

    def __repr__(self) -> str:
        return f'SectionSpan(p{self.page_num + 1}, y={self.y_top:.1f}, "{self.text}")'


@dataclass
class Config:
    """Tập hợp toàn bộ tham số chạy của script."""

    pdf_path:   str        = DEFAULT_PDF_PATH
    output_dir: str        = DEFAULT_OUTPUT_DIR
    scale:      float      = DEFAULT_SCALE
    keywords:   list[str]  = field(default_factory=lambda: list(SECTION_KEYWORDS))


# ---------------------------------------------------------------------------
# Tiện ích văn bản
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """
    Chuyển về chữ thường và loại bỏ dấu Unicode.

    Quy trình: lower → NFD decompose → loại ký tự thuộc category Mn
    (dấu kết hợp).

    Examples
    --------
    >>> normalize_text("Câu 1")
    'cau 1'
    >>> normalize_text("Bài tập")
    'bai tap'
    """
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text


def is_bold_span(span: dict) -> bool:
    """
    Kiểm tra xem một span có được định dạng in đậm hay không.

    Sử dụng hai tiêu chí độc lập (OR):
    - Bit flag 4 (0x10) trong trường ``flags`` của PyMuPDF.
    - Chuỗi "Bold" xuất hiện trong tên font.

    Parameters
    ----------
    span : dict
        Span dict trả về từ ``page.get_text("dict")``.
    """
    bold_by_flag = bool(span.get("flags", 0) & 16)
    bold_by_font = "Bold" in span.get("font", "")
    return bold_by_flag or bold_by_font


# ---------------------------------------------------------------------------
# Phát hiện section spans
# ---------------------------------------------------------------------------
def detect_section_spans(doc: fitz.Document, cfg: Config) -> list[SectionSpan]:
    """
    Quét toàn bộ document, trả về danh sách SectionSpan đã sắp xếp theo
    thứ tự xuất hiện (page_num tăng dần, y_top tăng dần).

    Thuật toán
    ----------
    1. Gom tất cả span in đậm vào bucket theo 3 ký tự đầu (normalised).
    2. Bucket đầu tiên phải chứa ít nhất một từ khoá trong ``cfg.keywords``.
    3. Chỉ những span có cùng x_left với span đầu tiên của bucket mới
       được chấp nhận (lọc nhiễu do các từ in đậm khác trong đề).
    4. Bucket có số phần tử nhiều nhất → đó là chuỗi tiêu đề section.

    Parameters
    ----------
    doc : fitz.Document
        Document PDF đã mở.
    cfg : Config
        Cấu hình chạy, dùng ``cfg.keywords``.

    Returns
    -------
    list[SectionSpan]
        Danh sách section đã sắp xếp, sẵn sàng để cắt ảnh.

    Raises
    ------
    ValueError
        Nếu không tìm thấy span nào phù hợp.
    """
    buckets: dict[str, list[dict]] = {}

    for page_index, page in enumerate(doc):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if not is_bold_span(span):
                        continue

                    norm   = normalize_text(span["text"])
                    prefix = norm[:3]
                    x0     = span["bbox"][0]

                    if not buckets:
                        # Bucket đầu tiên bắt buộc phải chứa từ khoá
                        if any(kw in norm for kw in cfg.keywords):
                            buckets[prefix] = [{**span, "page_num": page_index}]
                    else:
                        if prefix in buckets:
                            # Chỉ chấp nhận span có x_left khớp với span gốc
                            anchor_x = buckets[prefix][0]["bbox"][0]
                            if x0 == anchor_x:
                                buckets[prefix].append({**span, "page_num": page_index})
                        else:
                            buckets[prefix] = [{**span, "page_num": page_index}]

    if not buckets:
        raise ValueError(
            "Không tìm thấy span in đậm nào phù hợp với từ khoá: "
            + str(cfg.keywords)
        )

    best_prefix = max(buckets, key=lambda k: len(buckets[k]))
    raw_spans   = buckets[best_prefix]

    log.info(
        "Prefix được chọn: '%s'  —  %d section(s) tìm thấy.",
        best_prefix, len(raw_spans),
    )
    for raw in raw_spans:
        log.debug("  · [p%d] %s", raw["page_num"] + 1, raw["text"].strip())

    spans = [
        SectionSpan(
            text     = raw["text"].strip(),
            page_num = raw["page_num"],
            bbox     = tuple(raw["bbox"]),  # type: ignore[arg-type]
        )
        for raw in raw_spans
    ]
    spans.sort(key=lambda s: (s.page_num, s.y_top))
    return spans


# ---------------------------------------------------------------------------
# Tiện ích ảnh
# ---------------------------------------------------------------------------
def pix_to_pil(pix: fitz.Pixmap) -> Image.Image:
    """Chuyển đổi ``fitz.Pixmap`` sang ``PIL.Image`` thông qua PNG bytes."""
    return Image.open(io.BytesIO(pix.tobytes("png")))


def trim_vertical_whitespace(img: Image.Image) -> Image.Image:
    """
    Loại bỏ các hàng pixel thuần trắng (hoặc gần trắng) ở đầu và cuối ảnh.

    Đặc biệt hữu ích khi ghép nhiều trang: lề dưới trang trước và lề trên
    trang sau tạo ra một dải trắng rộng không mang nội dung.

    Ngưỡng quyết định được điều chỉnh qua hai hằng số toàn cục:
    ``WHITESPACE_THRESHOLD`` và ``WHITESPACE_MIN_CONTENT_PIXELS``.

    Parameters
    ----------
    img : PIL.Image.Image
        Ảnh đầu vào (bất kỳ mode nào, sẽ được chuyển sang grayscale nội bộ).

    Returns
    -------
    PIL.Image.Image
        Ảnh sau khi đã cắt khoảng trắng (cùng mode với ảnh đầu vào).
    """
    gray   = img.convert("L")
    pixels = list(gray.getdata())
    width, height = gray.size

    def row_has_content(y: int) -> bool:
        row = pixels[y * width : (y + 1) * width]
        return sum(1 for p in row if p < WHITESPACE_THRESHOLD) >= WHITESPACE_MIN_CONTENT_PIXELS

    # Tìm hàng đầu tiên có nội dung từ trên xuống
    top = 0
    while top < height and not row_has_content(top):
        top += 1

    # Tìm hàng cuối cùng có nội dung từ dưới lên
    bottom = height - 1
    while bottom > top and not row_has_content(bottom):
        bottom -= 1

    return img.crop((0, top, width, bottom + 1))


def stack_images_vertically(
    images:      list[Image.Image],
    trim_seams:  bool = True,
) -> Image.Image:
    """
    Ghép danh sách ảnh PIL theo chiều dọc thành một ảnh duy nhất.

    Parameters
    ----------
    images     : list[PIL.Image.Image]
        Danh sách ảnh đầu vào, theo thứ tự từ trên xuống dưới.
    trim_seams : bool
        Nếu ``True``, trim khoảng trắng đầu/cuối từng mảnh trước khi ghép
        để loại bỏ lề thừa tại vị trí nối trang.

    Returns
    -------
    PIL.Image.Image
        Ảnh kết quả (mode RGB, nền trắng).
    """
    if trim_seams:
        images = [trim_vertical_whitespace(img) for img in images]

    total_width  = max(img.width  for img in images)
    total_height = sum(img.height for img in images)

    combined = Image.new("RGB", (total_width, total_height), (255, 255, 255))
    y_offset = 0
    for img in images:
        combined.paste(img, (0, y_offset))
        y_offset += img.height

    return combined


# ---------------------------------------------------------------------------
# Thu thập pixmaps cho một section
# ---------------------------------------------------------------------------
def collect_pixmaps(
    doc:   fitz.Document,
    mat:   fitz.Matrix,
    start: SectionSpan,
    end:   Optional[SectionSpan],
) -> list[fitz.Pixmap]:
    """
    Trả về danh sách ``fitz.Pixmap`` tương ứng với nội dung của một section.

    Xử lý đầy đủ 3 trường hợp:

    1. **Cùng trang** (``start.page_num == end.page_num``):
       Một clip duy nhất từ ``y_start`` → ``y_end``.

    2. **Khác trang** (``start.page_num < end.page_num``):
       - Trang đầu : từ ``y_start`` → hết trang.
       - Trang giữa (0..N): toàn bộ trang.
       - Trang cuối: từ đầu trang → ``y_end``.

    3. **Section cuối** (``end is None``):
       Kéo từ ``y_start`` đến hết toàn bộ file.

    Parameters
    ----------
    doc   : fitz.Document
    mat   : fitz.Matrix  — ma trận scale đầu ra.
    start : SectionSpan  — tiêu đề bắt đầu section.
    end   : SectionSpan | None — tiêu đề kết thúc (span tiếp theo),
            hoặc ``None`` nếu đây là section cuối.

    Returns
    -------
    list[fitz.Pixmap]
        Các mảnh cần ghép theo thứ tự từ trên xuống dưới.
    """

    def clip_page(page_idx: int, y0: float, y1: Optional[float] = None) -> fitz.Pixmap:
        """Render một vùng clip của trang ``page_idx``."""
        page      = doc[page_idx]
        page_rect = page.rect
        y_bottom  = y1 if y1 is not None else page_rect.height
        clip      = fitz.Rect(0, y0, page_rect.width, y_bottom)
        return page.get_pixmap(matrix=mat, clip=clip)

    pixmaps: list[fitz.Pixmap] = []

    if end is None:
        # Trường hợp 3: section cuối — kéo đến hết file
        for p in range(start.page_num, doc.page_count):
            y0 = start.y_top if p == start.page_num else 0.0
            pixmaps.append(clip_page(p, y0))
        return pixmaps

    if start.page_num == end.page_num:
        # Trường hợp 1: cùng một trang
        pixmaps.append(clip_page(start.page_num, start.y_top, end.y_top))
        return pixmaps

    # Trường hợp 2: trải qua nhiều trang
    # 2a. Trang đầu: y_start → hết trang
    pixmaps.append(clip_page(start.page_num, start.y_top))

    # 2b. Các trang ở giữa: toàn bộ trang
    for p in range(start.page_num + 1, end.page_num):
        pixmaps.append(clip_page(p, 0.0))

    # 2c. Trang cuối: đầu trang → y_end
    pixmaps.append(clip_page(end.page_num, 0.0, end.y_top))

    return pixmaps


# ---------------------------------------------------------------------------
# Xuất section ra file ảnh
# ---------------------------------------------------------------------------
def export_section(
    doc:        fitz.Document,
    mat:        fitz.Matrix,
    index:      int,
    start:      SectionSpan,
    end:        Optional[SectionSpan],
    output_dir: str,
) -> str:
    """
    Cắt, ghép (nếu cần), trim whitespace và lưu một section ra file PNG.

    Parameters
    ----------
    doc        : fitz.Document
    mat        : fitz.Matrix
    index      : int   — số thứ tự section (bắt đầu từ 1), dùng đặt tên file.
    start      : SectionSpan — span bắt đầu của section.
    end        : SectionSpan | None — span kết thúc, hoặc None nếu cuối file.
    output_dir : str   — thư mục đầu ra.

    Returns
    -------
    str : đường dẫn tuyệt đối của file PNG đã lưu.
    """
    pixmaps = collect_pixmaps(doc, mat, start, end)
    images  = [pix_to_pil(pix) for pix in pixmaps]

    if len(images) == 1:
        final_image = trim_vertical_whitespace(images[0])
    else:
        # Ghép nhiều mảnh + trim khoảng trắng tại mỗi đường nối trang
        final_image = stack_images_vertically(images, trim_seams=True)

    filename  = f"section_{index:03d}_p{start.page_num + 1}.png"
    out_path  = os.path.join(output_dir, filename)
    final_image.save(out_path, format="PNG", optimize=True)
    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(cfg: Config) -> None:
    """
    Hàm chính: mở PDF → phát hiện sections → xuất ảnh.

    Parameters
    ----------
    cfg : Config
        Cấu hình chạy (có thể tạo thủ công hoặc từ ``parse_args()``).
    """
    if not os.path.isfile(cfg.pdf_path):
        raise FileNotFoundError(f"Không tìm thấy file PDF: '{cfg.pdf_path}'")

    os.makedirs(cfg.output_dir, exist_ok=True)
    log.info("Mở PDF : %s", os.path.abspath(cfg.pdf_path))
    log.info("Đầu ra : %s", os.path.abspath(cfg.output_dir))
    log.info("Scale  : %.1f×", cfg.scale)

    doc = fitz.open(cfg.pdf_path)
    mat = fitz.Matrix(cfg.scale, cfg.scale)

    spans = detect_section_spans(doc, cfg)
    total = len(spans)
    log.info("Bắt đầu xuất %d section(s)...", total)

    for i, span in enumerate(spans):
        end_span  = spans[i + 1] if i + 1 < total else None
        end_label = f"p{end_span.page_num + 1}" if end_span else "EOF"

        out_path = export_section(
            doc        = doc,
            mat        = mat,
            index      = i + 1,
            start      = span,
            end        = end_span,
            output_dir = cfg.output_dir,
        )
        log.info(
            "[%3d/%d]  p%d→%-4s  %-35s  →  %s",
            i + 1, total,
            span.page_num + 1,
            end_label,
            f'"{span.text}"',
            os.path.basename(out_path),
        )

    doc.close()
    log.info("Hoàn tất! Đã lưu %d ảnh tại: %s", total, os.path.abspath(cfg.output_dir))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> Config:
    """Phân tích tham số dòng lệnh và trả về Config tương ứng."""
    parser = argparse.ArgumentParser(
        description="Cắt PDF thành ảnh PNG theo từng section câu hỏi / bài tập.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Ví dụ:  python crop_pdf.py -i exam.pdf -o out/ -s 3.0",
    )
    parser.add_argument(
        "--input", "-i",
        default=DEFAULT_PDF_PATH,
        metavar="FILE",
        help="Đường dẫn file PDF đầu vào.",
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help="Thư mục lưu ảnh PNG đầu ra.",
    )
    parser.add_argument(
        "--scale", "-s",
        type=float,
        default=DEFAULT_SCALE,
        metavar="FLOAT",
        help="Hệ số phóng to (1.0 ≈ 72 dpi, 2.0 ≈ 144 dpi, 3.0 ≈ 216 dpi).",
    )
    parser.add_argument(
        "--keywords", "-k",
        nargs="+",
        default=SECTION_KEYWORDS,
        metavar="KW",
        help="Từ khoá nhận diện tiêu đề section (đã normalise).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Bật chế độ debug (in thêm thông tin chi tiết).",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    return Config(
        pdf_path   = args.input,
        output_dir = args.output,
        scale      = args.scale,
        keywords   = list(args.keywords),
    )


if __name__ == "__main__":
    run(parse_args())