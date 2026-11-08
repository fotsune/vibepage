# syntax=docker/dockerfile:1
# ────────────────────────────────────────────────────────────────────
#  Home — образ «только то, что нужно»: зависимости ставятся в отдельном
#  слое и без pip, в финальный образ попадают сам Python, пакеты и код.
#  Итог: ~60 МБ, один процесс, без компиляторов и временных файлов.
# ────────────────────────────────────────────────────────────────────
FROM python:3.12-alpine AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Flask и его зависимости — чистый Python либо musllinux-колёса,
# компилятор в образе не нужен.
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt \
 && rm -rf /tmp/requirements.txt \
           /opt/venv/lib/python3.*/site-packages/pip \
           /opt/venv/lib/python3.*/site-packages/pip-* \
           /opt/venv/lib/python3.*/site-packages/setuptools* \
           /opt/venv/lib/python3.*/site-packages/wheel* \
           /opt/venv/bin/pip /opt/venv/bin/pip3* /opt/venv/bin/wheel*


FROM python:3.12-alpine AS runtime

# ни .pyc на диске, ни буферизованного вывода
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOMEPAGE_CONFIG=/app/config/homepage.toml \
    HOMEPAGE_HOST=0.0.0.0 \
    HOMEPAGE_PORT=8080

WORKDIR /app

# uid 1000 — чтобы .icon-cache в смонтированной папке config не принадлежал root
RUN adduser -D -H -u 1000 -s /sbin/nologin app

COPY --from=deps /opt/venv /opt/venv
COPY --chown=app:app app.py ./
COPY --chown=app:app hp ./hp
COPY --chown=app:app templates ./templates
COPY --chown=app:app static ./static
COPY --chown=app:app config ./config

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('HOMEPAGE_PORT', '8080'), timeout=3)"

CMD ["python", "app.py"]
