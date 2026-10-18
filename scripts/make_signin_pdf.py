"""Regenerate docs/paper-signin-sheet.pdf (printable outage sign-in sheet)."""
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("docs/paper-signin-sheet.pdf", pagesize=landscape(letter))
w, h = landscape(letter)
c.setFont("Helvetica-Bold", 18)
c.drawString(36, h - 40, "GENE HAAS ADVANCED MANUFACTURING LAB — PAPER SIGN-IN SHEET (outage use)")
c.setFont("Helvetica", 10)
c.drawString(36, h - 58, "Date: ____________   Supervisor on duty: ______________________   Reason for paper mode: ______________________")
cols = [("Name", 130), ("Campus email", 150), ("Approval # (APR-)", 90), ("Machine", 90), ("Time in", 50), ("Time out", 50),
        ("Checklist (clean/tools/bench)", 90), ("Issues / damage", 100)]
x = 36
y0 = h - 80
for name, wd in cols:
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x + 2, y0 - 10, name)
    x += wd
for r in range(16):
    y = y0 - 18 - r * 26
    c.line(36, y, 36 + sum(wd for _, wd in cols), y)
x = 36
for _, wd in cols + [("", 0)]:
    c.line(x, y0 - 18 - 16 * 26, x, y0)
    x += wd
c.setFont("Helvetica-Oblique", 8)
c.drawString(36, 24, "Staff: enter these visits into Admin > Sessions/Reservations when the system is back. Verify certification and approval before allowing machine use.")
c.save()
