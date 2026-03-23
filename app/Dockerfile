FROM python:3.11-slim

RUN groupadd -g 999 scanner && useradd -r -u 999 -g scanner -d /app -s /sbin/nologin scanner

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scanner.py config.py ./
RUN chown -R scanner:scanner /app

USER scanner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

ENTRYPOINT ["python", "scanner.py"]
