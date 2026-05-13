FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV FLASK_ENV=production
ENV CONFIG_PATH=/app/config.yaml

EXPOSE 5050

# ADO_PAT must be supplied at runtime:
#   docker run -e ADO_PAT=<your-pat> -p 5050:5050 qa-dashboard
CMD ["python", "app.py"]
