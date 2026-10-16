"""/tv — public read-only live dashboard, plus the QR images it shows."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import board, lifecycle, qr
from app.web import render

router = APIRouter()


@router.get("/tv")
def tv_page(request: Request):
    return render(request, "tv.html", {})


@router.get("/tv/data")
def tv_data(db: Session = Depends(get_db)):
    lifecycle.lazy_sweep(db)  # persist any due no-shows/auto-closes; status itself never depends on this
    return JSONResponse(board.tv_payload(db), headers={"Cache-Control": "no-store"})


@router.get("/qr/machine/{mid}.png")
def qr_machine(mid: int, db: Session = Depends(get_db)):
    from app.models import Machine
    if db.get(Machine, mid) is None:
        raise HTTPException(404, "Unknown machine.")
    return Response(qr.png_for_path(f"/machine/{mid}"), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/qr/{name}.png")
def qr_png(name: str):
    try:
        data = qr.png(name)
    except KeyError:
        raise HTTPException(404, "Unknown code.") from None
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})
