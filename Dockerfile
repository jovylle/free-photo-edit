# One container serves the API and the static UI.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api ./api
COPY web ./web
COPY REVISION ./REVISION
# Dev defaults: fake provider (zero cost) and data outside the image.
# Future production deploys must override these explicitly.
ENV FREE_PHOTO_EDIT_ENGINE=fake \
    FREE_PHOTO_EDIT_DB=/app/data/credits.db \
    FREE_PHOTO_EDIT_STORAGE=/app/data/storage
EXPOSE 8080
# Docker marks the container unhealthy on a hang; the host monitor restarts it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/config', timeout=3)"]
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8080"]
