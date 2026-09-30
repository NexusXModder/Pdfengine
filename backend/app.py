from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import fitz, tempfile, os, io, json
from PIL import Image
import pytesseract

app = FastAPI(title="AetherPDF Engine", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
MAX_MB = 40

def rgb(c):
    return [(c >> 16) & 255, (c >> 8) & 255, c & 255]

def font_alias(name):
    n=(name or "").lower()
    if "courier" in n or "mono" in n: return "cour"
    if "times" in n or "serif" in n or "roman" in n: return "tiro"
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
    d=pytesseract.image_to_data(image,lang="eng+ben",config="--psm 3",
                                output_type=pytesseract.Output.DICT)
    out=[]
    for i,t in enumerate(d["text"]):
        t=(t or "").strip()
        try: conf=float(d["conf"][i])
        except: conf=-1
        if not t or conf < 25: continue
        x,y,w,h=[int(d[k][i]) for k in ("left","top","width","height")]
        out.append({
            "kind":"ocr","text":t,
            "bbox":[x/2,y/2,(x+w)/2,(y+h)/2],
            "font":"OCR","size":max(6,h/2*0.8),
            "color":[20,20,20],"confidence":round(conf,1)
        })
    return out

@app.get("/api/health")
def health(): return {"ok":True,"version":"0.2.0"}

@app.post("/api/analyze")
async def analyze(file: UploadFile=File(...)):
    raw=await file.read()
    if len(raw)>MAX_MB*1024*1024: raise HTTPException(413,f"PDF exceeds {MAX_MB} MB")
    if not raw.startswith(b"%PDF"): raise HTTPException(400,"Invalid PDF")
    f=tempfile.NamedTemporaryFile(delete=False,suffix=".pdf")
    f.write(raw); f.close()
    try:
        doc=fitz.open(f.name); pages=[]
        for n,page in enumerate(doc):
            items=digital_items(page)
            mode="digital"
            if not items:
                items=ocr_items(page); mode="ocr"
            pages.append({"page":n+1,"width":page.rect.width,"height":page.rect.height,
                          "items":items,"mode":mode})
        return {"page_count":len(doc),"pages":pages}
    finally:
        try: os.unlink(f.name)
        except: pass

class Edit(BaseModel):
    page:int
    bbox:list[float]
    replacement:str
    kind:str="digital"
    font:str="helv"
    size:float=10
    color:list[int]=[0,0,0]

@app.post("/api/edit")
async def edit(file:UploadFile=File(...),edits_json:str=""):
    raw=await file.read()
    if len(raw)>MAX_MB*1024*1024: raise HTTPException(413,f"PDF exceeds {MAX_MB} MB")
    try: edits=[Edit(**x) for x in json.loads(edits_json)]
    except Exception as e: raise HTTPException(400,f"Invalid edits: {e}")
    src=tempfile.NamedTemporaryFile(delete=False,suffix=".pdf")
    src.write(raw); src.close()
    out=tempfile.NamedTemporaryFile(delete=False,suffix=".pdf"); out.close()
    try:
        doc=fitz.open(src.name)
        for e in edits:
            if not 1<=e.page<=len(doc): continue
            r=fitz.Rect(*e.bbox)
            pad=max(.4,e.size*.035)
            rr=fitz.Rect(r.x0-pad,r.y0-pad,r.x1+pad,r.y1+pad)
            doc[e.page-1].add_redact_annot(rr,fill=(1,1,1))
        for p in doc: p.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        for e in edits:
            if not 1<=e.page<=len(doc): continue
            p=doc[e.page-1]; r=fitz.Rect(*e.bbox)
            c=tuple(max(0,min(255,int(v)))/255 for v in e.color[:3])
            p.insert_textbox(r,e.replacement,fontsize=max(4,min(96,e.size)),
                             fontname=font_alias(e.font),color=c,overlay=True)
        doc.save(out.name,garbage=4,deflate=True,clean=True)
        return FileResponse(out.name,media_type="application/pdf",
                            filename="aetherpdf-edited.pdf")
    finally:
        try: os.unlink(src.name)
        except: pass

@app.get("/")
def index(): return FileResponse("/app/frontend/index.html")

@app.get("/{path:path}")
def fallback(path:str):
    if path.startswith("api/"): raise HTTPException(404)
    return FileResponse("/app/frontend/index.html")
