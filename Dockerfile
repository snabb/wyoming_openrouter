# Wyoming OpenRouter Docker image (Alpine-based). This is a thin async HTTP
# client (requests + wyoming), no local ML inference, so unlike some other
# Wyoming server images there's no musl/glibc wheel-availability concern here
# and no need for a second Dockerfile variant.
# Supports: amd64, aarch64.

# ============================================
# BUILDER STAGE
# ============================================
FROM alpine:3.24 AS builder

# uv: Alpine's community repo ships it natively -- much faster than pip for
# dependency resolution/install.
RUN apk add --no-cache python3 uv git

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    UV_BREAK_SYSTEM_PACKAGES=1

COPY pyproject.toml .
COPY wyoming_openrouter/ wyoming_openrouter/
# requests pulls in real runtime deps of its own (urllib3/certifi/idna/
# charset-normalizer) -- installed without --no-deps so those resolve
# normally; the local package is then installed with --no-deps since its
# only dependencies (wyoming, requests) are already satisfied above.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install "wyoming>=1.7.0" "requests>=2.31.0"
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-deps .

# ============================================
# RUNTIME STAGE
# ============================================
FROM alpine:3.24

RUN apk add --no-cache \
    python3 \
    ca-certificates \
    netcat-openbsd \
    jq \
    curl

COPY --from=builder /usr/lib/python3.14/site-packages /usr/lib/python3.14/site-packages
COPY --from=builder /usr/bin/wyoming-openrouter /usr/bin/

WORKDIR /app

COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 10300

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD echo '{"type":"describe"}' | nc -w 5 localhost 10300 | grep -q "openrouter" || exit 1

CMD ["/run.sh"]
