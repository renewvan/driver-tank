FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY driver_tank/ driver_tank/
# Beside the module, where driver_info.py looks first (a checkout run uses the repo-root file).
COPY manifest.json driver_tank/manifest.json
COPY config.default.ini .

# The release workflow passes the git release tag. Unset (a local build) reports "dev".
ARG DRIVER_VERSION
ENV DRIVER_VERSION=${DRIVER_VERSION}

ENTRYPOINT ["python", "-m", "driver_tank.main"]
CMD ["--config", "/app/config.ini"]
