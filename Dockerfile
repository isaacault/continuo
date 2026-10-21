# continuo — one small ARM64/AMD64 image: python-slim, non-root, single uvicorn worker.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY continuo ./continuo
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DB_PATH=/data/continuo.db
COPY --from=build /install /usr/local
# /data holds the SQLite DB (+ .jwt_secret if JWT_SECRET is unset); bind-mount it with the same
# uid (compose sets user: PUID:PGID).
RUN useradd -m -u 1000 app && mkdir -p /data && chown app:app /data
USER app
WORKDIR /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/v1/health').status==200 else 1)"
# One worker: in-process SSE hub, rate limiters and loops assume a single process.
CMD ["sh", "-c", "exec uvicorn continuo.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log"]
