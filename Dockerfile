FROM python:3.12-alpine
WORKDIR /app

RUN addgroup -S bridge && adduser -S bridge -G bridge \
    && mkdir -p /data && chown bridge:bridge /data

COPY --chown=bridge:bridge requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=bridge:bridge bridge.py .

ENV ECOFLOW_DEVICE_UUID_FILE=/data/ecoflow_device_id

USER bridge
VOLUME /data

CMD ["python", "-u", "bridge.py"]
