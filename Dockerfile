FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends libjpeg62-turbo zlib1g \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

VOLUME ["/data"]
ENV MEURAL_CURATOR_CONFIG=/data/config.yaml

ENTRYPOINT ["meural-curator"]
CMD ["sync", "--config", "/data/config.yaml"]
