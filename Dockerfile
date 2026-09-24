FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY driver_tank/ driver_tank/
COPY config.default.ini .

ENTRYPOINT ["python", "-m", "driver_tank.main"]
CMD ["--config", "/app/config.ini"]
