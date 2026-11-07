from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Assignment:
    text: str
    images: list[bytes] = field(default_factory=list)
    source: str = "Введённый текст"
    course_id: int | None = None

    def image_urls(self):
        return ["data:image/png;base64," + base64.b64encode(x).decode("ascii") for x in self.images]


def image_bytes(data: bytes) -> bytes:
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((2000, 2000))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def load_assignment(path: Path) -> Assignment:
    if path.stat().st_size > 40 * 1024 * 1024:
        raise ValueError("Размер задания превышает 40 МБ")
    suffix = path.suffix.lower()
    if suffix in (".txt", ".json"):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_text(encoding="cp1251")
        return Assignment(text, source=str(path))
    if suffix in (".png", ".jpg", ".jpeg"):
        return Assignment("Изображение задания: " + path.name, [image_bytes(path.read_bytes())], str(path))
    if suffix == ".pdf":
        import pymupdf
        texts, images = [], []
        with pymupdf.open(path) as pdf:
            if pdf.needs_pass:
                raise ValueError("PDF защищён паролем")
            if len(pdf) > 30:
                raise ValueError("Разделите задание: поддерживается до 30 страниц PDF")
            for page in pdf:
                texts.append(page.get_text(sort=True))
                # Render every page: diagrams can be vector graphics, not embedded bitmaps.
                scale = min(1.5, 2000 / max(page.rect.width, page.rect.height))
                images.append(page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png"))
        text = "\n\n".join(texts)
        from .coursework import recognize_coursework
        return Assignment(text, images, str(path), recognize_coursework(path, text))
    if suffix == ".docx":
        from docx import Document
        from docx.oxml.ns import qn
        doc = Document(path)
        lines = []
        # Preserve paragraph/table order instead of flattening all tables at the end.
        for block in doc.element.body:
            if block.tag == qn("w:p"):
                lines.append("".join(t.text or "" for t in block.iter(qn("w:t"))))
            elif block.tag == qn("w:tbl"):
                for row in block.findall(qn("w:tr")):
                    lines.append(" | ".join(" ".join(t.text or "" for t in cell.iter(qn("w:t"))) for cell in row.findall(qn("w:tc"))))
        images = []
        for rel in doc.part.rels.values():
            if "image" in rel.reltype and not rel.is_external:
                try:
                    images.append(image_bytes(rel.target_part.blob))
                except Exception as exc:
                    raise ValueError("Не удалось прочитать изображение DOCX; преобразуйте его в PNG") from exc
        return Assignment("\n".join(lines), images, str(path))
    raise ValueError("Поддерживаются PDF, DOCX, PNG, JPG, TXT и JSON")
