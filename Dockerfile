FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY companion_memory ./companion_memory

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data
VOLUME ["/app/data"]
EXPOSE 8787

CMD ["companion-memory", "serve"]
