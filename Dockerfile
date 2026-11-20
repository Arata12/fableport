FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# openssh-client is required for `colab ssh --proxy-mode` + `ssh -L` port forwarding.
# Create UID/GID 1000 in /etc/passwd so OpenSSH accepts `user: "1000:1000"` in compose.yaml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 fableport \
    && useradd -u 1000 -g 1000 -m -s /bin/bash fableport

WORKDIR /app

RUN mkdir -p /app/output && chown -R 1000:1000 /app/output

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY tests ./tests

RUN --mount=type=cache,target=/root/.cache/pip pip install ".[colab]"

ENTRYPOINT ["fableport"]
CMD ["--help"]
