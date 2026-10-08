# A host-independent home for `sync`.
#
# Two things the container must not be allowed to decide for itself: what day
# it is, and where its state file goes. The first is handled in `clock.py`
# (Europe/Kyiv, hardcoded) and only backstopped by tzdata here. The second is
# what `WORKDIR /app` plus an editable install buys us -- see the README.
FROM python:3.13-slim

# `Europe/Kyiv` has to exist inside the image whatever the host is set to, and
# a slim base ships no timezone database at all.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# PYTHONUNBUFFERED is not a nicety here. Under Docker stdout is a pipe, so
# Python block-buffers it, and a watch loop that prints only when something
# changed would look identical to a hung one.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# `PROJECT_ROOT` is derived from the location of `config.py` -- three levels up
# -- so the layout of this directory is load-bearing. With the source at
# `/app/src/tapo_scheduler/` it resolves to `/app`, which is where `.env` and
# `.state/` are expected. Installed non-editably it would resolve to
# site-packages, and a failed state write is silent: every restart would
# re-post the schedule to the channel. Hence `-e` and the layout below.
WORKDIR /app

# `pyproject.toml` reads `readme` and `license` from disk, so both files have
# to be here before the install, not after it.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install -e .

# A named volume mounted at /app/.state inherits this directory's ownership,
# which is why it is created (and chowned) by the image rather than by Docker.
RUN useradd --uid 10001 --no-create-home tapo \
 && mkdir -p /app/.state \
 && chown -R tapo:tapo /app
USER tapo

CMD ["python", "-m", "tapo_scheduler.sync", "--watch"]
