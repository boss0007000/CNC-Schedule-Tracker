"""PDF generation (reportlab) for approval records and the printable QR sheet."""
import io

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from app.services import notify


def approval_pdf(a) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    y = h - 0.9 * inch
    c.setFont("Helvetica-Bold", 20)
    c.drawString(0.9 * inch, y, "Gene Haas Advanced Manufacturing Lab: Approval Record")
    y -= 0.5 * inch
    c.setFont("Helvetica-Bold", 28)
    c.drawString(0.9 * inch, y, a.number)
    y -= 0.5 * inch
    c.setFont("Helvetica", 11)
    for line in notify.approval_text(a).split("\n")[1:]:
        for part in simpleSplit(line, "Helvetica", 11, w - 1.8 * inch) or [""]:
            c.drawString(0.9 * inch, y, part)
            y -= 16
            if y < inch:
                c.showPage()
                y = h - inch
                c.setFont("Helvetica", 11)
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(0.9 * inch, 0.7 * inch, "Bring this number when reserving a machine. Valid only while status is active.")
    c.save()
    return buf.getvalue()


def _qr_reader(name: str, box: int = 14):
    from reportlab.lib.utils import ImageReader

    from app.services import qr
    return ImageReader(io.BytesIO(qr.png(name, box)))


def qr_sheet_pdf(shop_name: str) -> bytes:
    """One US-Letter page, all four codes in a 2x2 grid, with labels and instructions."""
    from app.services import qr
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(w / 2, h - 0.7 * inch, shop_name)
    c.setFont("Helvetica", 11)
    c.drawCentredString(w / 2, h - 0.95 * inch, "Scan with your phone camera. No app or account needed — you'll verify your campus email.")
    cell_w, cell_h = (w - 1.5 * inch) / 2, (h - 2.0 * inch) / 2
    for i, (key, label, hint, path) in enumerate(qr.FLOWS):
        col, row = i % 2, i // 2
        x0 = 0.75 * inch + col * cell_w
        y0 = h - 1.3 * inch - (row + 1) * cell_h
        size = min(cell_w, cell_h) - 1.3 * inch
        c.drawImage(_qr_reader(key), x0 + (cell_w - size) / 2, y0 + 0.9 * inch, size, size)
        c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(x0 + cell_w / 2, y0 + 0.55 * inch, f"{i + 1}. {label}")
        c.setFont("Helvetica", 10)
        for j, line in enumerate(simpleSplit(hint, "Helvetica", 10, cell_w - 0.4 * inch)):
            c.drawCentredString(x0 + cell_w / 2, y0 + 0.35 * inch - j * 12, line)
        c.setFont("Helvetica-Oblique", 8)
        c.drawCentredString(x0 + cell_w / 2, y0 + 0.1 * inch, qr.url_for(path))
    c.save()
    return buf.getvalue()


def qr_poster_pdf(name: str) -> bytes:
    """One full page per code (tape it next to the machine or door)."""
    from app.services import qr
    f = qr.flow(name)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 44)
    c.drawCentredString(w / 2, h - 1.3 * inch, f[1])
    c.drawImage(_qr_reader(name, 20), (w - 5.5 * inch) / 2, h - 7.3 * inch, 5.5 * inch, 5.5 * inch)
    c.setFont("Helvetica", 18)
    for j, line in enumerate(simpleSplit(f[2], "Helvetica", 18, w - 2 * inch)):
        c.drawCentredString(w / 2, h - 8.0 * inch - j * 24, line)
    c.setFont("Helvetica-Oblique", 12)
    c.drawCentredString(w / 2, h - 9.2 * inch, qr.url_for(f[3]))
    c.save()
    return buf.getvalue()


def machine_qr_poster_pdf(machine_name: str, png: bytes, url: str) -> bytes:
    """One page for taping beside a machine."""
    from reportlab.lib.utils import ImageReader
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 40)
    c.drawCentredString(w / 2, h - 1.2 * inch, machine_name)
    c.setFont("Helvetica-Bold", 26)
    c.drawCentredString(w / 2, h - 1.8 * inch, "Scan to check in")
    c.drawImage(ImageReader(io.BytesIO(png)), (w - 5.5 * inch) / 2, h - 7.6 * inch, 5.5 * inch, 5.5 * inch)
    c.setFont("Helvetica", 16)
    c.drawCentredString(w / 2, h - 8.3 * inch, "Reserved this machine? Scan, enter the TV code, and tap Check in.")
    c.drawCentredString(w / 2, h - 8.7 * inch, "No reservation? Book one first at the Reserve a Machine code.")
    c.setFont("Helvetica-Oblique", 9)
    c.drawCentredString(w / 2, 0.6 * inch, url)
    c.save()
    return buf.getvalue()
