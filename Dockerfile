FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unixodbc \
    && curl -fsSLo /tmp/packages-microsoft-prod.deb \
        https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && rm /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

RUN addgroup --system app \
    && adduser --system --ingroup app app \
    && install -d -o app -g app -m 0700 /var/lib/project-intelligence

COPY --chown=app:app requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --disable-pip-version-check -r requirements.txt

COPY --chown=app:app pyproject.toml README.md ./
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
RUN python -m pip install --disable-pip-version-check --no-deps .
RUN python -m scripts.generate_sbom /opt/project-intelligence-backend.cdx.json

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; request=urllib.request.Request('http://127.0.0.1:8000/health', headers={'X-Forwarded-Proto':'https'}); urllib.request.urlopen(request, timeout=2)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
