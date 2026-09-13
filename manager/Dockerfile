# Small Python image for the Valheim manager backend + WebUI.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Runtime dependencies only. The test suite reads repo-root files that are outside
# this build context, so it runs from a checkout (requirements-dev.txt), and pytest
# has no business in the image the manager runs from.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tools ./tools

# The manager never needs root; it talks to Docker over the socket proxy only.
# /srv/state and /srv/settings exist here so that the named state volume inherits
# this ownership when Docker initialises it -- otherwise it would be created
# root-owned and an unprivileged manager could not store the admin account in it.
RUN groupadd --system --gid 10001 manager \
    && useradd --system --create-home --uid 10001 --gid 10001 manager \
    && mkdir -p /srv/state /srv/settings \
    && chown -R manager:manager /srv \
    && chmod 0700 /srv/state
USER manager

EXPOSE 8080

CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
