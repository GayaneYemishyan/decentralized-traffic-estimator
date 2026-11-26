# Build from the repo root:  docker build -f orchestration/Dockerfile .
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
WORKDIR /app

COPY orchestration/requirements.txt orchestration/requirements.txt
RUN pip install --no-cache-dir -r orchestration/requirements.txt

# Layout matters: serializer/ledger find contracts/ at <root>/contracts, and
# inference/ + cv_module/ resolve it the same way.
COPY contracts ./contracts
COPY inference ./inference
COPY cv_module ./cv_module
COPY orchestration ./orchestration

RUN useradd --system --uid 10001 app && mkdir /data && chown app /data \
    && chmod +x /app/orchestration/entrypoint.sh
USER app
VOLUME /data
WORKDIR /app/orchestration
ENTRYPOINT ["/app/orchestration/entrypoint.sh"]
CMD ["--startup-timeout", "60"]