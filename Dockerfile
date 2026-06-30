FROM python:3.14-slim

ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=$GIT_COMMIT

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ ./scripts/
COPY app/     ./app/
COPY static/  ./static/

RUN mkdir -p data

EXPOSE 8000
CMD ["sh", "-c", "python scripts/build_sde.py && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
