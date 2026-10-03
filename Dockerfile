# The read-only JobPilot API, as deployed to Azure Container Apps.
# Same Python as CI (3.14), slim variant: no compilers or docs, so a smaller image with less to patch.
FROM python:3.14-slim

# unbuffered: log lines reach Azure's log stream at once, not when a buffer fills;
# no .pyc files: the code never changes inside a running container, so they'd be dead weight;
# JOBPILOT_DOCS=0: /docs, /redoc and /openapi.json closed unless someone deliberately opens them (fail closed)
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 JOBPILOT_DOCS=0

WORKDIR /app

# dependencies before the code: Docker caches this layer, so a code-only change doesn't reinstall them
COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

COPY jobpilot/ jobpilot/

# a non-root user: if someone ever broke into the app, they couldn't change the image's files
RUN useradd --system --no-create-home app
USER app

EXPOSE 8000

# 0.0.0.0 inside the container so Azure's ingress can reach it (locally the README keeps 127.0.0.1);
# --no-server-header: don't advertise the server software and version to anyone probing the API
CMD ["uvicorn", "jobpilot.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
