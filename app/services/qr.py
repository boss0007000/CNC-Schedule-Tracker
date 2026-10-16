"""QR codes for the four stable student URLs. Printed codes never change: the URL paths are fixed."""
import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from app.config import get_config

FLOWS = [
    ("submit", "Submit a Project", "Get a project approved for shop machines", "/submit"),
    ("reserve", "Reserve a Machine", "Book a time slot with your approval number", "/reserve"),
    ("checkin", "Check In", "Arrived? Enter your reservation and the TV code", "/checkin"),
    ("checkout", "Check Out", "Done? Finish the cleanup checklist", "/checkout"),
]
_cache: dict[tuple[str, int], bytes] = {}


def url_for(path: str) -> str:
    return get_config().base_url.rstrip("/") + path


def flow(name: str):
    return next((f for f in FLOWS if f[0] == name), None)


def png_for_path(path: str, box_size: int = 8) -> bytes:
    """QR for any same-site path (used for the per-machine schedule/contact codes on the TV)."""
    key = (url_for(path), box_size)
    if key not in _cache:
        q = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=box_size, border=2)
        q.add_data(key[0])
        q.make(fit=True)
        buf = io.BytesIO()
        q.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
        _cache[key] = buf.getvalue()
    return _cache[key]


def png(name: str, box_size: int = 10) -> bytes:
    f = flow(name)
    if f is None:
        raise KeyError(name)
    key = (url_for(f[3]), box_size)
    if key not in _cache:
        q = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=box_size, border=3)
        q.add_data(key[0])
        q.make(fit=True)
        buf = io.BytesIO()
        q.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
        _cache[key] = buf.getvalue()
    return _cache[key]
