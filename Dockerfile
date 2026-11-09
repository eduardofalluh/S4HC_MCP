# S4HC_MCP as a container: Streamable HTTP on port 8000, endpoint /mcp.
# Configuration comes from environment variables only (see docs/GENAI_STUDIO_DEPLOYMENT.md);
# no .env file or credential is baked into the image.
FROM python:3.14.8-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

WORKDIR /app

# Dependencies first, so a code change does not reinstall them. Everything from requirements.txt's
# "# Test only" line to the end stays out of the image, so keep test-only pins in that last section.
COPY requirements.txt ./
RUN sed '/^# Test only/,$d' requirements.txt > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY server.py ./
COPY src/ ./src/

# Never run as root.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

EXPOSE 8000

# The server has no health route; a refused connection means it is down. Dial MCP_HOST itself unless it
# is a wildcard address, which listens on loopback too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, socket; h = os.environ['MCP_HOST']; socket.create_connection(('127.0.0.1' if h in ('0.0.0.0', '::', '') else h, int(os.environ['MCP_PORT'])), 3).close()"

# Host and port come from MCP_HOST / MCP_PORT. On a non-loopback host the server needs S4HC_MCP_API_KEY, a
# sign-in setup (S4HC_OIDC_* or S4HC_SAP_AUTH=user_token) or S4HC_MCP_ALLOW_UNAUTHENTICATED=true, and exits
# with code 2 otherwise, so a deployment without any of them fails at once instead of serving without auth.
CMD ["python", "server.py", "--http"]
