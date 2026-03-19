FROM python:3.11-slim

RUN groupadd -g 999 scanner && useradd -r -u 999 -g scanner -d /app -s /sbin/nologin scanner

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN chown -R scanner:scanner /app
USER scanner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "scanner.py"]
