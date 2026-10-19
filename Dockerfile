FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY node_tank/ node_tank/
# Beside the module, where node_info.py looks first (a checkout run uses the repo-root file).
COPY manifest.json node_tank/manifest.json
COPY config.default.ini .

# The release workflow passes the git release tag. Unset (a local build) reports "dev".
ARG NODE_VERSION
ENV NODE_VERSION=${NODE_VERSION}

ENTRYPOINT ["python", "-m", "node_tank.main"]
CMD ["--config", "/app/config.ini"]
