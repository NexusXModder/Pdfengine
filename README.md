# AetherPDF Engine v0.2
Unified PDF editor foundation for single-container deployment.

- Frontend: HTML/CSS/JS + PDF.js
- Backend: FastAPI + PyMuPDF + Pillow + Tesseract OCR
- One Docker image serves the UI and API.
- Digital/scanned pages are automatically detected behind one unified editor.

Run:
docker build -t aetherpdf .
docker run --rm -p 8000:8000 aetherpdf

This is a high-fidelity foundation, not a guarantee of pixel-identical editing for every PDF. Embedded/subset fonts, unusual encodings, clipping, transparency and scanned backgrounds can require specialized processing.
