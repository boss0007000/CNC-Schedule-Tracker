FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
RUN useradd -r -u 10001 shop && mkdir -p /srv/var && chown -R shop /srv/var
USER shop
EXPOSE 8000
# Migrate, then serve. ONE worker on purpose: the in-process scheduler runs once, and free tiers have little RAM.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
