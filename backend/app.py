from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import fitz
import io, json, os, glob, tempfile
from PIL import Image
import pytesseract

APP_VERSION = "1.0.0"
MAX_MB = 40

app = FastAPI(title="AetherPDF", version=APP_VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def rgb(c: int):
    return [(c >> 16) & 255, (c >> 8) & 255, c & 255]


def has_unicode(text: str):
    return any(ord(ch) > 255 for ch in (text or ""))


def find_font(patterns):
    roots = [
        "/usr/share/fonts/truetype/noto",
        "/usr/share/fonts/opentype/noto",
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/truetype/freefont",
    ]
    for root in roots:
        for pattern in patterns:
            hits = glob.glob(os.path.join(root, pattern))
            if hits:
                return hits[0]
    return None


def fallback_font(original: str = "", replacement: str = ""):
    n = (original or "").lower()
    if has_unicode(replacement):
        if "bold" in n:
            return find_font(["NotoSansBengali-Bold.ttf", "NotoSans-Bold.ttf", "DejaVuSans-Bold.ttf"])
        if "italic" in n or "oblique" in n:
            return find_font(["NotoSansBengali-Regular.ttf", "NotoSans-Italic.ttf", "DejaVuSans-Oblique.ttf"])
        return find_font(["NotoSansBengali-Regular.ttf", "NotoSans-Regular.ttf", "DejaVuSans.ttf"])
    return None


def font_alias(name: str):
    n = (name or "").lower()
    if "courier" in n or "mono" in n:
        return "cour"
    if "times" in n or "serif" in n or "roman" in n:
        if "bold" in n and ("italic" in n or "oblique" in n): return "tibi"
        if "bold" in n: return "tibo"
        if "italic" in n or "oblique" in n: return "tiit"
        return "tiro"
    if "bold" in n and ("italic" in n or "oblique" in n): return "hebi"
    if "bold" in n: return "hebo"
    if "italic" in n or "oblique" in n: return "heit"
    return "helv"


def digital_items(page):
    out = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for s in line.get("spans", []):
                text = s.get("text", "")
                if not text.strip():
                    continue
                out.append({
                    "kind": "digital",
                    "text": text,
                    "bbox": list(s["bbox"]),
                    "font": s.get("font", ""),
                    "size": float(s.get("size", 10)),
                    "color": rgb(s.get("color", 0)),
                    "flags": int(s.get("flags", 0)),
                })
    return out


def render(page, scale=2):
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def ocr_items(page):
    image = render(page, 2)
    d = pytesseract.image_to_data(image, lang="eng+ben", config="--psm 3", output_type=pytesseract.Output.DICT)
    out = []
    for i, t in enumerate(d["text"]):
        t = (t or "").strip()
        try: conf = float(d["conf"][i])
        except Exception: conf = -1
        if not t or conf < 25:
            continue
        x, y, w, h = [int(d[k][i]) for k in ("left", "top", "width", "height")]
        out.append({
            "kind": "ocr", "text": t,
            "bbox": [x / 2, y / 2, (x + w) / 2, (y + h) / 2],
            "font": "OCR", "size": max(6, h / 2 * 0.8),
            "color": [20, 20, 20], "confidence": round(conf, 1)
        })
    return out


def background_color(page, rect):
    """Estimate a local background without removing surrounding line art."""
    try:
        r = fitz.Rect(rect)
        pad = max(2.0, min(7.0, max(r.width, r.height) * 0.10))
        clip = fitz.Rect(max(0, r.x0-pad), max(0, r.y0-pad), min(page.rect.width, r.x1+pad), min(page.rect.height, r.y1+pad))
        pix = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), clip=clip, alpha=False)
        im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        w, h = im.size
        pixels = []
        for x in range(w):
            pixels.append(im.getpixel((x, 0)))
            if h > 1: pixels.append(im.getpixel((x, h-1)))
        for y in range(1, max(1, h-1)):
            pixels.append(im.getpixel((0, y)))
            if w > 1: pixels.append(im.getpixel((w-1, y)))
        if not pixels:
            return (1, 1, 1)
        pixels.sort(key=lambda p: sum(p))
        p = pixels[len(pixels)//2]
        return tuple(v/255 for v in p)
    except Exception:
        return (1, 1, 1)


def embedded_font_file(page, font_name, cache):
    key = (font_name or "")
    if key in cache:
        return cache[key]
    target = key.split(",")[0].strip().lower()
    try:
        for item in page.get_fonts(full=True):
            xref = item[0]
            base = (item[3] or "").lower()
            resource = (item[4] or "").lower()
            # Prefer exact resource/base matches, but allow the only usable font on the page.
            if target and target not in base and target not in resource:
                continue
            extracted = page.parent.extract_font(xref)
            if not extracted or not extracted[3]:
                continue
            ext = (extracted[1] or "ttf").lower()
            if ext not in ("ttf", "otf"):
                continue
            path = os.path.join(tempfile.gettempdir(), f"aetherpdf-font-{xref}.{ext}")
            with open(path, "wb") as fh:
                fh.write(extracted[3])
            cache[key] = path
            return path
    except Exception:
        pass
    cache[key] = None
    return None


class Edit(BaseModel):
    page: int
    bbox: list[float]
    replacement: str
    kind: str = "digital"
    font: str = "helv"
    size: float = 10
    color: list[int] = [0, 0, 0]


def insert_replacement(page, e, rect, font_cache):
    text = e.replacement or ""
    color = tuple(max(0, min(255, int(v))) / 255 for v in e.color[:3])
    fontfile = None if e.kind == "digital" and not has_unicode(text) else fallback_font(e.font, text)
    embedded = embedded_font_file(page, e.font, font_cache) if not fontfile else None

    def attempt(size):
        common = dict(rect=rect, buffer=text, fontsize=max(4, min(96, float(size))), color=color, align=fitz.TEXT_ALIGN_LEFT, overlay=True)
        if embedded:
            try:
                page.insert_font(fontname="AetherOrig", fontfile=embedded)
                return page.insert_textbox(fontname="AetherOrig", **common)
            except Exception:
                return page.insert_textbox(fontfile=embedded, **common)
        if fontfile:
            return page.insert_textbox(fontfile=fontfile, **common)
        return page.insert_textbox(fontname=font_alias(e.font), **common)

    result = attempt(e.size)
    if result < 0:
        result = attempt(max(4, float(e.size) * 0.86))
    if result < 0:
        result = attempt(max(4, float(e.size) * 0.72))
    if result < 0:
        raise ValueError(f"Replacement does not fit the selected text on page {e.page}. Try shorter text.")


def build_pdf(raw, edits):
    doc = fitz.open(stream=raw, filetype="pdf")
    jobs = []
    font_cache = {}
    for e in edits:
        if not 1 <= e.page <= len(doc):
            raise ValueError(f"Invalid page number: {e.page}")
        r = fitz.Rect(*e.bbox)
        if r.is_empty or r.width <= 0 or r.height <= 0:
            raise ValueError("Invalid text bounding box.")
        # Keep the cover very close to the original text bounds. Graphics are explicitly preserved.
        pad_x = min(0.9, max(0.15, r.width * 0.012))
        pad_y = min(0.65, max(0.12, r.height * 0.018))
        cover = fitz.Rect(r.x0-pad_x, r.y0-pad_y, r.x1+pad_x, r.y1+pad_y)
        bg = background_color(doc[e.page-1], cover)
        jobs.append((e, r, cover, bg))

    # Remove only text in the selected areas. Keep vector graphics/lines intact.
    for e, r, cover, bg in jobs:
        doc[e.page-1].add_redact_annot(cover, fill=bg)
    for p in doc:
        p.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_REMOVE)

    for e, r, cover, bg in jobs:
        insert_replacement(doc[e.page-1], e, r, font_cache)

    out = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return out


def validate_pdf(raw):
    if len(raw) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"PDF exceeds {MAX_MB} MB")
    if not raw.startswith(b"%PDF"):
        raise HTTPException(400, "Invalid PDF")


@app.get("/api/health")
def health():
    return {"ok": True, "version": APP_VERSION}


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    raw = await file.read()
    validate_pdf(raw)
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        pages = []
        for n, page in enumerate(doc):
            items = digital_items(page)
            mode = "digital"
            if not items:
                items = ocr_items(page)
                mode = "ocr"
            pages.append({"page": n+1, "width": page.rect.width, "height": page.rect.height, "items": items, "mode": mode})
        doc.close()
        return {"page_count": len(pages), "pages": pages}
    except Exception as e:
        raise HTTPException(400, f"PDF analysis failed: {e}")


@app.post("/api/edit")
async def edit(file: UploadFile = File(...), edits_json: str = Form("")):
    raw = await file.read()
    validate_pdf(raw)
    try:
        parsed = json.loads(edits_json or "[]")
        edits = [Edit(**x) for x in parsed]
    except Exception as e:
        raise HTTPException(400, f"Invalid edit data: {e}")
    if not edits:
        raise HTTPException(400, "No edits were supplied.")
    try:
        output = build_pdf(raw, edits)
        return Response(content=output, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="aetherpdf-edited.pdf"'})
    except Exception as e:
        raise HTTPException(500, f"PDF edit failed: {e}")


@app.get("/")
def index():
    return FileResponse("/app/frontend/index.html")


@app.get("/{path:path}")
def fallback(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse("/app/frontend/index.html")
