FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        gcc \
        g++ \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt

COPY . /app
RUN chmod +x /app/deploy/docker-entrypoint.sh \
    && groupadd --system --gid 10001 shopkeeper \
    && useradd --system --uid 10001 --gid shopkeeper --home-dir /app shopkeeper \
    && mkdir -p /var/lib/shopkeeper/staging /var/lib/shopkeeper/state /var/lib/shopkeeper/uploads \
    && chown -R shopkeeper:shopkeeper /app /var/lib/shopkeeper

USER shopkeeper

EXPOSE 8000
ENTRYPOINT ["/app/deploy/docker-entrypoint.sh"]
CMD ["python", "-m", "knowledge.main"]
