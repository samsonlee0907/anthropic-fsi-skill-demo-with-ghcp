FROM python:3.13-slim

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sec_edgar_http_server.py .
COPY financial_fact_pack.py .

ENV PORT=8080
EXPOSE 8080

# Streamable-HTTP host with shared-secret header auth (see module docstring).
CMD ["python", "sec_edgar_http_server.py"]
