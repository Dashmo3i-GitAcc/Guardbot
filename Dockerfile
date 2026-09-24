FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The Admin Control Center's dependencies, in their own layer.
#
# This is the same image the bot runs from — not a second one. The split exists
# purely for layer reuse: `requirements.txt` above is the heavy layer, and a
# dashboard dependency added here costs one small layer instead of invalidating
# and re-downloading the whole bot install. `app/web` is imported only by
# `python -m app.web`, so the bot's own process never touches these packages.
COPY requirements-dashboard.txt .
RUN pip install --no-cache-dir -r requirements-dashboard.txt

COPY app ./app

# The one-time MTProto session bootstrap (tools/voice_live_session.py). Copied
# because it has to run *inside* this container: the session it creates must land
# in /data, which is the mounted volume, and the libraries it needs (telethon)
# are installed here and not necessarily on the host.
#
# Only the source is copied. The session file itself is never in the image: it
# is created at runtime, under /data, and .dockerignore/gitignore both exclude
# the data directory — a credential baked into a layer would be readable by
# anyone who can pull the image, and would survive every rotation.
COPY tools ./tools

# Operator tooling for the panel (ops/dashboard_passwd.py). Copied for the same
# reason as tools/: the credential it writes must land in /data, and the app
# modules it imports need the bot's environment, which only exists here.
COPY ops ./ops

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
