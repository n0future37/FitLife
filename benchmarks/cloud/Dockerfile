FROM mirror.ccs.tencentyun.com/library/python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
WORKDIR /app
COPY pyproject.toml ./
RUN python -c "import subprocess,sys,tomllib; p=tomllib.load(open('pyproject.toml','rb')); subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir','--index-url','https://mirrors.cloud.tencent.com/pypi/simple','setuptools==84.2.1',*p['project']['dependencies']])"
ENV PYTHONPATH=/app
COPY fitlife ./fitlife
COPY migrations ./migrations
COPY alembic.ini ./
COPY benchmarks/cloud ./benchmarks/cloud
RUN pip install --no-deps --no-build-isolation . && useradd --uid 10001 --create-home fitlife && mkdir -p artifacts data && chown -R fitlife:fitlife artifacts data
USER fitlife
CMD ["python", "-m", "benchmarks.cloud.serve", "--workers", "2"]
