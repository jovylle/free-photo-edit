# One container serves the API and the static UI.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api ./api
COPY web ./web
# Dev defaults: fake provider (zero cost) and data outside the image.
# Future production deploys must override these explicitly.
ENV FREE_PHOTO_EDIT_ENGINE=fake \
    FREE_PHOTO_EDIT_DB=/app/data/credits.db \
    FREE_PHOTO_EDIT_STORAGE=/app/data/storage
EXPOSE 8080
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8080"]
