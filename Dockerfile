# S4HC_MCP as a container: Streamable HTTP on port 8000, endpoint /mcp.
# Configuration comes from environment variables only (see docs/GENAI_STUDIO_DEPLOYMENT.md);
# no .env file or credential is baked into the image.
FROM python:3.14.8-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY server.py ./
COPY src/ ./src/

# Never run as root.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

EXPOSE 8000

# The server has no health route; a refused connection means it is down.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), 3).close()"

# Binding 0.0.0.0 requires S4HC_MCP_API_KEY (or S4HC_MCP_ALLOW_UNAUTHENTICATED=true); the server exits with
# code 2 otherwise, so a deployment without either fails at once instead of serving without auth.
CMD ["python", "server.py", "--http", "--host", "0.0.0.0", "--port", "8000"]
