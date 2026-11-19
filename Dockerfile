FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends libjpeg62-turbo zlib1g \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

VOLUME ["/data"]
ENV REDDIT_MEURAL_CONFIG=/data/config.yaml

ENTRYPOINT ["reddit-meural"]
CMD ["sync", "--config", "/data/config.yaml"]
