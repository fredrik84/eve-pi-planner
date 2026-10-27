FROM python:3.15.2-slim

ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=$GIT_COMMIT

WORKDIR /srv/app

COPY requirements.txt .
# HiGHS may need compiling before wheels reach a new Python release. Keep the
# toolchain temporary; only its shared runtime libraries remain in the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libstdc++6 libgomp1 \
    && CMAKE_BUILD_PARALLEL_LEVEL=2 pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/ ./scripts/
COPY app/     ./app/
COPY static/  ./static/

RUN mkdir -p data

EXPOSE 8000
ENV UVICORN_WORKERS=1
CMD ["sh", "-c", "python scripts/build_sde.py && python scripts/populate_geo.py && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --workers ${UVICORN_WORKERS}"]
