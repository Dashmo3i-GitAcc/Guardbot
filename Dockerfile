FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Prove the real voice-chat transport is loadable *here*, at build time.
#
# `app/voice_live/telegram_voice.py` imports these three by name and only ever
# lazily, so a broken wheel would not surface at import of the bot — it would
# surface at the first join, mid-call, which is the worst possible moment. The
# native half (`ntgcalls`) is the one that can fail for a reason no Python-level
# check catches: a wheel built for the wrong ABI, or linked against a libstdc++
# this base image does not carry. Importing it now turns that into a failed
# build rather than a failed call.
#
# This does not weaken the runtime's graceful degradation: the transport still
# reports `library_missing` and the bot still boots if the packages are ever
# absent from an environment. It only guarantees that *this* image — the one
# built from this file — is one that can hold a call.
RUN python -c "import telethon, pytgcalls, ntgcalls; from importlib.metadata import version; print('voice transport importable:', version('telethon'), version('py-tgcalls'), version('ntgcalls'))"

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

# The build's identity, so a running container can say which commit produced
# the behaviour being investigated.
#
# The image deliberately contains no `.git` (`.dockerignore` excludes it), so the
# running process cannot read its own version from the working tree. This writes
# the commit the image was built from to a plain file instead, and
# `app/observe/context.py` reads it as the `deployment_id` every recorded event
# carries. That is what turns "the assistant started doing X" into "the assistant
# started doing X on build abc123", which is the difference between an
# investigation and a guess.
#
# `GIT_SHA` has no default that could be mistaken for a real commit: an unset
# build records `unknown`, and `unknown` is visibly not a version.
ARG GIT_SHA=unknown
RUN printf '%s\n' "$GIT_SHA" > /srv/BUILD_INFO

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
