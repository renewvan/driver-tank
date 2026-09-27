FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY node_tank/ node_tank/
COPY config.default.ini .

ENTRYPOINT ["python", "-m", "node_tank.main"]
CMD ["--config", "/app/config.ini"]
