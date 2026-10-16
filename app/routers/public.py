from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import settings as shop_settings
from app.web import render

from fastapi.responses import PlainTextResponse

router = APIRouter()


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    """Keep search engines away from pages that show names and phone numbers."""
    return "User-agent: *\nDisallow: /schedule\nDisallow: /machine/\nDisallow: /my\nDisallow: /admin\nDisallow: /tv\n"


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    s = shop_settings.load(db)
    return render(request, "home.html", shop_name=s.shop_name, emergency=s.emergency_active,
                  emergency_message=s.emergency_message)
