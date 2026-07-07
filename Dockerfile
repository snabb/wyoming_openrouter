# Wyoming OpenRouter Docker image (Alpine-based). This is a thin async HTTP
# client (requests + wyoming), no local ML inference, so unlike some other
# Wyoming server images there's no musl/glibc wheel-availability concern here
# and no need for a second Dockerfile variant. mpg123 is the one non-Python
# runtime dependency: a small (~1.7 MB with libs), purpose-built MPEG audio
# decoder used to convert mp3 to raw PCM for TTS tasks whose model doesn't
# support response_format=pcm directly (Wyoming always needs raw PCM).
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
    jq \
    curl \
    mpg123

COPY --from=builder /usr/lib/python3.14/site-packages /usr/lib/python3.14/site-packages
COPY --from=builder /usr/bin/wyoming-openrouter /usr/bin/

WORKDIR /app

COPY run.sh /run.sh
COPY healthcheck.sh /healthcheck.sh
RUN chmod +x /run.sh /healthcheck.sh

# A fixed range of 20 ports is reserved (config.yaml's `ports:` section
# mirrors this) since each task listens on its own dedicated port and
# Supervisor/Docker both need ports declared upfront rather than discovered
# at runtime. Covers the entire live OpenRouter STT+TTS catalog (~19 models
# as of writing) with a little headroom.
EXPOSE 10300-10319

# start-period no longer needs to cover the model-catalog fetch at all:
# __main__.py runs it as a background TaskGroup member rather than
# awaiting it before any port binds, so a slow/timed-out OpenRouter
# response can no longer delay startup -- confirmed live that every
# task's port binds in well under 2s regardless. 10s leaves headroom for
# container/interpreter startup itself, not for catalog fetching.
HEALTHCHECK --interval=5s --timeout=10s --start-period=10s --retries=3 \
    CMD /healthcheck.sh

CMD ["/run.sh"]
