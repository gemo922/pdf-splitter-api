"""
PDF Splitter API — Service REST de découpe, extraction et fusion de PDF.
Stack : FastAPI + pypdf
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PDF Splitter API",
    version="1.0.0",
    description=(
        "Service de découpe, extraction, fusion et transformation de fichiers PDF. "
        "Endpoints : info, extract, every, ranges, size, merge, rotate, delete, reorder."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_pdf(data: bytes) -> PdfReader:
    """Ouvre un PDF depuis des bytes, lève 400 si invalide."""
    try:
        return PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PDF invalide : {exc}")


def _writer_to_bytes(writer: PdfWriter) -> bytes:
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _parse_pages(spec: str, page_count: int) -> list[int]:
    """
    Parse une spécification de pages → liste d'indices 0-based.

    Formats (combinables avec virgules) :
      "3"      → page 3
      "2-5"    → pages 2 à 5
      "5-"     → page 5 à la fin
      "-3"     → pages 1 à 3
      "last"   → dernière page
      "odd"    → impaires
      "even"   → paires

    Numéros en base 1 (humain).
    """
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if part == "last":
            result.add(page_count - 1)
        elif part == "odd":
            result.update(range(0, page_count, 2))
        elif part == "even":
            result.update(range(1, page_count, 2))
        elif "-" in part:
            left, right = part.split("-", 1)
            start = int(left) if left else 1
            end = int(right) if right else page_count
            if start < 1 or end < 1 or start > page_count:
                raise HTTPException(
                    status_code=400,
                    detail=f"Plage invalide '{part}' pour un PDF de {page_count} pages.",
                )
            end = min(end, page_count)
            result.update(range(start - 1, end))
        else:
            p = int(part)
            if p < 1 or p > page_count:
                raise HTTPException(
                    status_code=400,
                    detail=f"Page {p} hors limites (1-{page_count}).",
                )
            result.add(p - 1)
    return sorted(result)


def _extract_pages(reader: PdfReader, indices: list[int]) -> bytes:
    """Extrait les pages données (indices 0-based) dans un nouveau PDF."""
    writer = PdfWriter()
    for i in indices:
        writer.add_page(reader.pages[i])
    return _writer_to_bytes(writer)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# 1. Info ───────────────────────────────────────────────────────────────────

@app.post("/info", summary="Informations sur un PDF", tags=["Info"])
async def pdf_info(file: UploadFile = File(...)):
    """Retourne le nombre de pages, les métadonnées et la taille du fichier."""
    data = await file.read()
    reader = _read_pdf(data)
    meta = reader.metadata
    pages_info = []
    for i, page in enumerate(reader.pages):
        box = page.mediabox
        w = float(box.width)
        h = float(box.height)
        pages_info.append({
            "page": i + 1,
            "width_pt": round(w, 2),
            "height_pt": round(h, 2),
            "width_mm": round(w * 25.4 / 72, 1),
            "height_mm": round(h * 25.4 / 72, 1),
            "rotation": page.get("/Rotate", 0),
        })
    metadata = {}
    if meta:
        for key in ("title", "author", "subject", "creator", "producer"):
            val = getattr(meta, key, None)
            if val:
                metadata[key] = val
        if meta.creation_date:
            metadata["creation_date"] = str(meta.creation_date)
        if meta.modification_date:
            metadata["modification_date"] = str(meta.modification_date)

    return {
        "filename": file.filename,
        "size_bytes": len(data),
        "page_count": len(reader.pages),
        "metadata": metadata,
        "pages": pages_info,
    }


# 2. Extraction de pages ───────────────────────────────────────────────────

@app.post("/split/extract", summary="Extraire des pages spécifiques", tags=["Découpe"])
async def split_extract(
    file: UploadFile = File(...),
    pages: str = Form(
        ...,
        description=(
            "Pages à extraire. Ex: '1,3,5', '2-8', '1-3,7,10-', "
            "'odd', 'even', 'last'. Numérotation à partir de 1."
        ),
    ),
):
    """Extrait les pages demandées dans un nouveau PDF."""
    data = await file.read()
    reader = _read_pdf(data)
    indices = _parse_pages(pages, len(reader.pages))
    if not indices:
        raise HTTPException(status_code=400, detail="Aucune page sélectionnée.")
    result = _extract_pages(reader, indices)
    return Response(
        content=result,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="extrait.pdf"'},
    )


# 3. Split en morceaux de N pages ──────────────────────────────────────────

@app.post("/split/every", summary="Découper toutes les N pages", tags=["Découpe"])
async def split_every(
    file: UploadFile = File(...),
    n: int = Form(1, ge=1, description="Nombre de pages par morceau (défaut : 1)."),
):
    """Découpe le PDF en morceaux de N pages. Retourne un ZIP si plusieurs morceaux."""
    data = await file.read()
    reader = _read_pdf(data)
    total = len(reader.pages)
    stem = Path(file.filename or "document").stem

    files: dict[str, bytes] = {}
    part = 1
    for start in range(0, total, n):
        end = min(start + n, total)
        indices = list(range(start, end))
        pdf_bytes = _extract_pages(reader, indices)
        files[f"{stem}_part{part:03d}.pdf"] = pdf_bytes
        part += 1

    if len(files) == 1:
        name, content = next(iter(files.items()))
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return Response(
        content=_make_zip(files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}_split.zip"'},
    )


# 4. Split par plages ─────────────────────────────────────────────────────

@app.post("/split/ranges", summary="Découper par plages de pages", tags=["Découpe"])
async def split_ranges(
    file: UploadFile = File(...),
    ranges: str = Form(
        ...,
        description=(
            "Plages séparées par des points-virgules. "
            "Ex: '1-3;4-6;7-10'. Chaque plage produit un PDF."
        ),
    ),
):
    """Découpe le PDF selon les plages données. Retourne un ZIP."""
    data = await file.read()
    reader = _read_pdf(data)
    stem = Path(file.filename or "document").stem
    total = len(reader.pages)

    range_specs = [r.strip() for r in ranges.split(";") if r.strip()]
    if not range_specs:
        raise HTTPException(status_code=400, detail="Aucune plage spécifiée.")

    files: dict[str, bytes] = {}
    for spec in range_specs:
        indices = _parse_pages(spec, total)
        if not indices:
            continue
        pdf_bytes = _extract_pages(reader, indices)
        first = indices[0] + 1
        last = indices[-1] + 1
        files[f"{stem}_p{first}-{last}.pdf"] = pdf_bytes

    if not files:
        raise HTTPException(status_code=400, detail="Aucune page extraite.")

    if len(files) == 1:
        name, content = next(iter(files.items()))
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return Response(
        content=_make_zip(files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}_ranges.zip"'},
    )


# 5. Split par taille approximative ───────────────────────────────────────

@app.post("/split/size", summary="Découper par taille maximale", tags=["Découpe"])
async def split_by_size(
    file: UploadFile = File(...),
    max_mb: float = Form(10.0, gt=0, description="Taille max par fichier en Mo."),
):
    """Découpe le PDF pour que chaque morceau ne dépasse pas ~max_mb Mo."""
    data = await file.read()
    reader = _read_pdf(data)
    total = len(reader.pages)
    stem = Path(file.filename or "document").stem
    max_bytes = int(max_mb * 1024 * 1024)

    files: dict[str, bytes] = {}
    part = 1
    start = 0

    while start < total:
        end = start + 1
        last_good = _extract_pages(reader, [start])

        while end < total:
            candidate = _extract_pages(reader, list(range(start, end + 1)))
            if len(candidate) > max_bytes and end > start:
                break
            last_good = candidate
            end += 1

        first_p = start + 1
        last_p = end
        files[f"{stem}_part{part:03d}_p{first_p}-{last_p}.pdf"] = last_good
        part += 1
        start = end

    if len(files) == 1:
        name, content = next(iter(files.items()))
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return Response(
        content=_make_zip(files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}_sized.zip"'},
    )


# 6. Fusion ────────────────────────────────────────────────────────────────

@app.post("/merge", summary="Fusionner plusieurs PDF", tags=["Fusion"])
async def merge_pdfs(files: list[UploadFile] = File(...)):
    """Fusionne plusieurs PDF en un seul, dans l'ordre d'envoi."""
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Au moins 2 fichiers requis.")

    writer = PdfWriter()
    for f in files:
        data = await f.read()
        reader = _read_pdf(data)
        for page in reader.pages:
            writer.add_page(page)

    return Response(
        content=_writer_to_bytes(writer),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="merged.pdf"'},
    )


# 7. Rotation ──────────────────────────────────────────────────────────────

@app.post("/rotate", summary="Pivoter des pages", tags=["Transformation"])
async def rotate_pages(
    file: UploadFile = File(...),
    pages: str = Form("all", description="Pages à pivoter. 'all' pour toutes."),
    angle: int = Form(90, description="Angle : 90, 180 ou 270."),
):
    """Pivote les pages sélectionnées et retourne le PDF modifié."""
    if angle not in (90, 180, 270):
        raise HTTPException(status_code=400, detail="Angle doit être 90, 180 ou 270.")

    data = await file.read()
    reader = _read_pdf(data)
    total = len(reader.pages)

    if pages.strip().lower() == "all":
        indices = set(range(total))
    else:
        indices = set(_parse_pages(pages, total))

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i in indices:
            page = page.rotate(angle)
        writer.add_page(page)

    return Response(
        content=_writer_to_bytes(writer),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="rotated.pdf"'},
    )


# 8. Suppression de pages ─────────────────────────────────────────────────

@app.post("/delete", summary="Supprimer des pages", tags=["Transformation"])
async def delete_pages(
    file: UploadFile = File(...),
    pages: str = Form(..., description="Pages à supprimer."),
):
    """Supprime les pages indiquées et retourne le PDF restant."""
    data = await file.read()
    reader = _read_pdf(data)
    total = len(reader.pages)
    to_delete = set(_parse_pages(pages, total))

    if len(to_delete) >= total:
        raise HTTPException(status_code=400, detail="Impossible de supprimer toutes les pages.")

    to_keep = [i for i in range(total) if i not in to_delete]
    result = _extract_pages(reader, to_keep)

    return Response(
        content=result,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="trimmed.pdf"'},
    )


# 9. Réordonner ────────────────────────────────────────────────────────────

@app.post("/reorder", summary="Réordonner les pages", tags=["Transformation"])
async def reorder_pages(
    file: UploadFile = File(...),
    order: str = Form(
        ...,
        description="Nouvel ordre (base 1, virgules). Ex: '3,1,2,5,4'. Répétitions et omissions possibles.",
    ),
):
    """Réordonne les pages selon l'ordre spécifié."""
    data = await file.read()
    reader = _read_pdf(data)
    total = len(reader.pages)

    try:
        indices = [int(x.strip()) - 1 for x in order.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Format d'ordre invalide.")

    for i in indices:
        if i < 0 or i >= total:
            raise HTTPException(
                status_code=400,
                detail=f"Page {i + 1} hors limites (1-{total}).",
            )

    result = _extract_pages(reader, indices)
    return Response(
        content=result,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="reordered.pdf"'},
    )


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Système"])
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
