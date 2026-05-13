FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as non-root
RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV FLASK_ENV=production
ENV FLASK_DEBUG=false
ENV CONFIG_PATH=/app/config.yaml

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:5050/api/health', timeout=4)" \
    || exit 1

# ADO_PAT must be supplied at runtime:
#   docker run -e ADO_PAT=<your-pat> -p 5050:5050 qa-dashboard
CMD ["python", "app.py"]
