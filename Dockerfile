FROM python:3.12-alpine

# NUT server (upsd + dummy-ups driver) comes from the Alpine package
RUN apk add --no-cache nut \
    && addgroup -S bridge && adduser -S bridge -G bridge \
    && mkdir -p /data /etc/nut /var/run/nut \
    && chown bridge:bridge /data

WORKDIR /app

COPY --chown=bridge:bridge requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=bridge:bridge bridge.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV ECOFLOW_DEVICE_UUID_FILE=/data/ecoflow_device_id
ENV NUT_HOST=127.0.0.1
ENV NUT_PORT=3493

EXPOSE 3493
EXPOSE 3494

VOLUME /data

ENTRYPOINT ["/entrypoint.sh"]
