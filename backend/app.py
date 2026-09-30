from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import fitz
import io, json, os, glob, re
from PIL import Image
import pytesseract

app = FastAPI(title="AetherPDF Engine", version="0.5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
MAX_MB = 40

def rgb(c):
    return [(c >> 16) & 255, (c >> 8) & 255, c & 255]

def has_unicode(text):
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

def fallback_font(original="", replacement=""):
    n = (original or "").lower()
    if has_unicode(replacement):
        if "bold" in n:
            p = find_font(["NotoSansBengali-Bold.ttf", "NotoSans-Bold.ttf", "DejaVuSans-Bold.ttf"])
        elif "italic" in n or "oblique" in n:
            p = find_font(["NotoSans-Italic.ttf", "DejaVuSans-Oblique.ttf"])
        else:
            p = find_font(["NotoSansBengali-Regular.ttf", "NotoSans-Regular.ttf", "DejaVuSans.ttf"])
        return p
    return None

def font_alias(name):
    n=(name or "").lower()
    if "courier" in n or "mono" in n: return "cour"
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
    out=[]
    for block in page.get_text("dict").get("blocks",[]):
        if block.get("type") != 0: continue
        for line in block.get("lines",[]):
            for s in line.get("spans",[]):
                text=s.get("text","")
                if not text.strip(): continue
                out.append({
                    "kind":"digital","text":text,"bbox":list(s["bbox"]),
                    "font":s.get("font",""),"size":float(s.get("size",10)),
                    "color":rgb(s.get("color",0)),
                    "flags":int(s.get("flags",0))
                })
    return out

def render(page, scale=2):
    pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")

def ocr_items(page):
    image=render(page,2)
    d=pytesseract.image_to_data(
        image, lang="eng+ben", config="--psm 3",
        output_type=pytesseract.Output.DICT
    )
    out=[]
    for i,t in enumerate(d["text"]):
        t=(t or "").strip()
        try: conf=float(d["conf"][i])
        except Exception: conf=-1
        if not t or conf < 25: continue
        x,y,w,h=[int(d[k][i]) for k in ("left","top","width","height")]
        out.append({
            "kind":"ocr","text":t,
            "bbox":[x/2,y/2,(x+w)/2,(y+h)/2],
            "font":"OCR","size":max(6,h/2*0.8),
            "color":[20,20,20],"confidence":round(conf,1)
        })
    return out

def background_color(page, rect):
    """Estimate the local background around a text box from the original page."""
    try:
        r=fitz.Rect(rect)
        pad=max(2.0,min(8.0,max(r.width,r.height)*0.12))
        clip=fitz.Rect(max(0,r.x0-pad),max(0,r.y0-pad),
                       min(page.rect.width,r.x1+pad),min(page.rect.height,r.y1+pad))
        pix=page.get_pixmap(matrix=fitz.Matrix(1.5,1.5),clip=clip,alpha=False)
        im=Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        w,h=im.size
        pixels=[]
        # Border pixels are less likely to contain the foreground text.
        for x in range(w):
            pixels.append(im.getpixel((x,0)))
            if h>1: pixels.append(im.getpixel((x,h-1)))
        for y in range(1,max(1,h-1)):
            pixels.append(im.getpixel((0,y)))
            if w>1: pixels.append(im.getpixel((w-1,y)))
        if not pixels: return (1,1,1)
        pixels.sort(key=lambda p: sum(p))
        mid=pixels[len(pixels)//2]
        return tuple(v/255 for v in mid)
    except Exception:
        return (1,1,1)

@app.get("/api/health")
def health():
    return {"ok":True,"version":"0.5.0"}

@app.post("/api/analyze")
async def analyze(file:UploadFile=File(...)):
    raw=await file.read()
    if len(raw)>MAX_MB*1024*1024:
        raise HTTPException(413,f"PDF exceeds {MAX_MB} MB")
    if not raw.startswith(b"%PDF"):
        raise HTTPException(400,"Invalid PDF")
    try:
        doc=fitz.open(stream=raw,filetype="pdf")
        pages=[]
        for n,page in enumerate(doc):
            items=digital_items(page)
            mode="digital"
            if not items:
                items=ocr_items(page)
                mode="ocr"
            pages.append({
                "page":n+1,"width":page.rect.width,"height":page.rect.height,
                "items":items,"mode":mode
            })
        doc.close()
        return {"page_count":len(pages),"pages":pages}
    except Exception as e:
        raise HTTPException(400,f"PDF analysis failed: {e}")

class Edit(BaseModel):
    page:int
    bbox:list[float]
    replacement:str
    kind:str="digital"
    font:str="helv"
    size:float=10
    color:list[int]=[0,0,0]

@app.post("/api/edit")
async def edit(file:UploadFile=File(...),edits_json:str=Form("")):
    raw=await file.read()
    if len(raw)>MAX_MB*1024*1024:
        raise HTTPException(413,f"PDF exceeds {MAX_MB} MB")
    try:
        parsed=json.loads(edits_json or "[]")
        edits=[Edit(**x) for x in parsed]
    except Exception as e:
        raise HTTPException(400,f"Invalid edit data: {e}")
    if not edits:
        raise HTTPException(400,"No edits were supplied.")

    try:
        doc=fitz.open(stream=raw,filetype="pdf")

        # Snapshot local backgrounds before changing the page.
        jobs=[]
        for e in edits:
            if not 1<=e.page<=len(doc):
                raise ValueError(f"Invalid page number: {e.page}")
            r=fitz.Rect(*e.bbox)
            if r.is_empty or r.width<=0 or r.height<=0:
                raise ValueError("Invalid text bounding box.")
            bg=background_color(doc[e.page-1],r)
            jobs.append((e,r,bg))

        # Remove the old text/graphic in each selected region.
        for e,r,bg in jobs:
            pad=max(.35,min(2.5,e.size*.035))
            rr=fitz.Rect(r.x0-pad,r.y0-pad,r.x1+pad,r.y1+pad)
            doc[e.page-1].add_redact_annot(rr,fill=bg)

        for p in doc:
            p.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # Insert replacements. Use the original family for simple Latin text;
        # use a Unicode-capable embedded fallback when necessary.
        for e,r,bg in jobs:
            page=doc[e.page-1]
            color=tuple(max(0,min(255,int(v)))/255 for v in e.color[:3])
            text=e.replacement or ""
            fontfile=fallback_font(e.font,text)

            kwargs=dict(
                rect=r, buffer=text,
                fontsize=max(4,min(96,float(e.size))),
                color=color, align=fitz.TEXT_ALIGN_LEFT, overlay=True
            )

            if fontfile:
                result=page.insert_textbox(fontfile=fontfile, **kwargs)
            else:
                result=page.insert_textbox(fontname=font_alias(e.font), **kwargs)

            # If the selected box is too small for the replacement, retry with
            # a smaller size instead of silently producing an empty result.
            if result < 0:
                retry_size=max(4,float(e.size)*0.82)
                if fontfile:
                    result=page.insert_textbox(fontfile=fontfile,
                                               fontsize=retry_size,
                                               **{k:v for k,v in kwargs.items() if k!="fontsize"})
                else:
                    result=page.insert_textbox(fontname=font_alias(e.font),
                                               fontsize=retry_size,
                                               **{k:v for k,v in kwargs.items() if k!="fontsize"})
            if result < 0:
                raise ValueError(
                    f"Replacement does not fit the selected text box on page {e.page}. "
                    f"Try shorter text."
                )

        output=doc.tobytes(garbage=4,deflate=True,clean=True)
        doc.close()
        return Response(
            content=output,
            media_type="application/pdf",
            headers={"Content-Disposition":'attachment; filename="aetherpdf-edited.pdf"'}
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500,f"PDF export failed: {e}")

@app.get("/")
def index():
    return FileResponse("/app/frontend/index.html")

@app.get("/{path:path}")
def fallback(path:str):
    if path.startswith("api/"): raise HTTPException(404)
    return FileResponse("/app/frontend/index.html")
