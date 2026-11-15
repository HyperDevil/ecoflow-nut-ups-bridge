#!/bin/sh
set -e

echo "[INIT] Rendering NUT configuration..."

# ---- ups.conf: the dummy-ups driver owns the virtual UPS ----
cat > /etc/nut/ups.conf <<EOF
[ecoflow-ups]
    driver = dummy-ups
    port = /etc/nut/ecoflow-ups.dev
    desc = "EcoFlow via MQTT bridge"
    pollinterval = 1
EOF

# ---- Initial device state (bridge overwrites these every message) ----
cat > /etc/nut/ecoflow-ups.dev <<EOF
ups.mfr: Ecoflow
ups.model: River 3 UPS
ups.status: OL
battery.charge: 100
battery.runtime: 3600
input.voltage: 230.0
input.frequency: 50
output.voltage: 230.0
output.frequency: 50
battery.temperature: 32.0
ups.realpower: 100.0
ups.load: 15
EOF

# ---- upsd.conf: listen on all interfaces so TrueNAS can connect ----
cat > /etc/nut/upsd.conf <<EOF
LISTEN 0.0.0.0 3493
EOF

# ---- upsd.users: one account for the bridge (SET), one for monitors ----
cat > /etc/nut/upsd.users <<EOF
[${NUT_USERNAME}]
    password = ${NUT_PASSWORD}
    actions = SET
    instcmds = all

[upsmonuser]
    password = ${NUT_UPSMON_PASSWORD}
    upsmon master
EOF

# ---- nut.conf: we are a netserver ----
cat > /etc/nut/nut.conf <<EOF
MODE=netserver
EOF

chown -R nut:nut /etc/nut /var/run/nut
chmod 640 /etc/nut/upsd.users

echo "[INIT] Starting dummy-ups driver for ecoflow-ups..."
DRIVER=$(find /usr/lib /usr/libexec -type f -name dummy-ups 2>/dev/null | head -n1)
if [ -z "$DRIVER" ]; then
    echo "[FATAL] dummy-ups driver binary not found" >&2
    exit 1
fi
"$DRIVER" -a ecoflow-ups -u nut &

echo "[INIT] Starting upsd..."
upsd -u nut

# Wait until upsd answers before handing over to the bridge
i=0
until upsc ecoflow-ups@localhost >/dev/null 2>&1; do
    i=$((i+1))
    if [ "$i" -ge 30 ]; then
        echo "[FATAL] upsd did not become ready in 30s" >&2
        exit 1
    fi
    sleep 1
done
echo "[INIT] upsd is serving ecoflow-ups."

echo "[INIT] Starting EcoFlow MQTT bridge..."
exec python -u bridge.py
