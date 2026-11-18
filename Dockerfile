# syntax=docker/dockerfile:1
FROM python:3.12.16-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
  python -c "import subprocess,sys,tomllib; p=tomllib.load(open('pyproject.toml','rb')); subprocess.check_call([sys.executable,'-m','pip','install','setuptools==84.2.1',*p['project']['dependencies']])"
COPY fitlife ./fitlife
RUN pip install --no-deps --no-build-isolation . && useradd --uid 10001 --create-home fitlife
COPY migrations ./migrations
COPY alembic.ini ./
COPY config ./config
RUN mkdir -p /app/artifacts /app/data && chown -R fitlife:fitlife /app/artifacts /app/data
USER fitlife
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/ready',timeout=8)"
CMD ["python", "-m", "fitlife.server", "--environment", "dev", "--host", "0.0.0.0"]

FROM base AS media
USER root
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
  && apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=2 update \
  && apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=2 install -y --no-install-recommends libgl1 libegl1 libgles2 libglib2.0-0 libportaudio2 \
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --timeout 60 --retries 2 --no-build-isolation '.[media]'
# MediaPipe imports lazily; verify native EGL/GLES linkage without model weights.
RUN python -c "import ctypes; from pathlib import Path; import mediapipe; ctypes.CDLL(str(Path(mediapipe.__file__).parent / 'tasks/c/libmediapipe.so'))"
USER fitlife

FROM media AS test
ENV PYTEST_ADDOPTS="-o cache_dir=/app/artifacts/pytest-cache"
USER root
RUN --mount=type=cache,target=/root/.cache/pip pip install '.[dev]'
COPY tests ./tests
COPY evals ./evals
COPY scripts ./scripts
USER fitlife
CMD ["python", "-m", "pytest", "-p", "pytest_asyncio.plugin", "--integration", "-q"]

FROM media AS runtime
