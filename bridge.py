import base64
import os
import ssl
import struct
import threading
import time
import uuid
import requests
import paho.mqtt.client as mqtt

# ==========================================
# CONFIGURATION (all from environment variables)
# ==========================================
def env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    if not val and not default:
        raise SystemExit(f"[CONFIG ERROR] Required environment variable {name} is not set")
    return val

# EcoFlow account
EMAIL         = env("ECOFLOW_USERNAME")
PASSWORD      = env("ECOFLOW_PASSWORD")
API_BASE_URL  = env("ECOFLOW_API_BASE_URL", "https://api-e.ecoflow.com")

# Device: EcoFlow serial number ("device id") and max wattage for ups.load %
DEVICE_SN     = env("ECOFLOW_DEVICE_ID")
NUT_UPS_NAME  = env("NUT_UPS_NAME", "ecoflow-ups")
MAX_WATTS     = int(env("ECOFLOW_MAX_WATTS", "300"))

# Where the simulated Android device UUID is persisted (volume-mounted in Docker)
DEVICE_UUID_FILE = env("ECOFLOW_DEVICE_UUID_FILE", "/data/ecoflow_device_id")

# Path to the dummy-ups definition file the bridge feeds by rewriting.
# The dummy-ups driver re-reads this file whenever its timestamp changes
# (dummy-once mode). This replaces the previous SET VAR approach, which
# Alpine's dummy-ups build rejects with ERR READONLY.
NUT_DEV_FILE = env("NUT_DEV_FILE", f"/etc/nut/{NUT_UPS_NAME}.dev")

# Low battery threshold (%) to trigger OB LB (On Battery + Low Battery) for graceful server shutdown
LOW_BATTERY_PCT = int(env("LOW_BATTERY_PCT", "20"))

# Credential-refresh behaviour
AUTH_FAIL_LIMIT      = int(env("AUTH_FAIL_LIMIT", "3"))
MAX_CONNECT_ATTEMPTS = int(env("MAX_CONNECT_ATTEMPTS", "5"))
NETWORK_RETRY_DELAY  = int(env("NETWORK_RETRY_DELAY", "30"))
REAUTH_RETRY_DELAY   = int(env("REAUTH_RETRY_DELAY", "60"))

UPS_MAPPING = { DEVICE_SN: {"nut_name": NUT_UPS_NAME, "max_watts": MAX_WATTS } }

DEVICE_STATES = {sn: {} for sn in UPS_MAPPING}

# ==========================================
# DUMMY-UPS STATE WRITER
# ==========================================
# dummy-ups reads "variable: value" lines from the .dev definition file.
# In dummy-once mode it re-reads the file only when its mtime changes, so
# every update must (a) contain the full desired state, and (b) touch the
# timestamp. Writes are atomic (tmp file + rename) so the driver never sees
# a half-written file.
IDENTITY_VARS = {
    "device.mfr": "Ecoflow",
    "device.model": "River 3 UPS",
    "ups.mfr": "Ecoflow",
    "ups.model": "River 3 UPS",
}

class NutDevFileWriter:
    def __init__(self, path: str):
        self.path = path
        self.state = dict(IDENTITY_VARS)

    def _load_existing(self):
        """Seed state from whatever the entrypoint template put there."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    if ":" in line:
                        key, _, val = line.partition(":")
                        self.state.setdefault(key.strip(), val.strip())
        except OSError:
            pass

    def update_vars(self, variables: dict):
        self.state.update(variables)
        tmp_path = f"{self.path}.tmp"
        payload = "".join(f"{k}: {v}\n" for k, v in self.state.items())
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, self.path)
        os.utime(self.path, None)  # bump mtime: signals dummy-ups to re-read

dev_writer = NutDevFileWriter(NUT_DEV_FILE)
dev_writer._load_existing()

# ==========================================
# ANDROID-SIMULATED ECOFLOW AUTH
# ==========================================
def get_persistent_device_uuid() -> str:
    if os.path.exists(DEVICE_UUID_FILE):
        with open(DEVICE_UUID_FILE, "r", encoding="utf-8") as f:
            saved_id = f.read().strip()
            if saved_id:
                return saved_id
    new_id = str(uuid.uuid4())
    with open(DEVICE_UUID_FILE, "w", encoding="utf-8") as f:
        f.write(new_id)
    return new_id

def get_app_mqtt_credentials() -> dict:
    headers = {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "application/json",
        "Content-Type": "application/json; charset=UTF-8",
        "lang": "en_US",
        "platform": "android",
    }

    encoded_pw = base64.b64encode(PASSWORD.encode("utf-8")).decode("utf-8")
    login_payload = {
        "os": "android",
        "scene": "IOT_APP",
        "appVersion": "6.17.7.12",
        "osVersion": "16",
        "password": encoded_pw,
        "oauth": {"bundleId": "com.ecoflow.iot"},
        "email": EMAIL,
        "userType": "ECOFLOW",
    }

    login_resp = requests.post(f"{API_BASE_URL}/auth/login", headers=headers, json=login_payload, timeout=10)
    login_resp.raise_for_status()
    login_data = login_resp.json()
    if str(login_data.get("code")) != "0":
        raise RuntimeError(f"App login failed: {login_data}")

    token = login_data["data"]["token"]
    user_id = login_data["data"]["user"]["userId"]

    cert_headers = {**headers, "authorization": f"Bearer {token}"}
    cert_resp = requests.get(
        f"{API_BASE_URL}/iot-auth/app/certification",
        headers=cert_headers,
        params={"userId": user_id},
        timeout=10,
    )
    cert_resp.raise_for_status()
    cert_data = cert_resp.json()
    if str(cert_data.get("code")) != "0":
        raise RuntimeError(f"MQTT certification failed: {cert_data}")

    creds = cert_data["data"]
    creds["userId"] = user_id
    return creds

# ==========================================
# PROTOBUF WIRE PARSER & XOR DECRYPTOR
# ==========================================
# River 3 / R655 Protobuf Field Map
RIVER3_FIELDS = {
    3: ("pow_in_sum_w", "float"),
    4: ("pow_out_sum_w", "float"),
    8: ("ac_freq_hz", "int"),
    54: ("pow_get_ac_in", "float"),
    55: ("pow_get_ac_hv_out", "float"),
    227: ("ac_out_volts", "int"),
    242: ("bms_batt_soc", "float"),
    255: ("bms_chg_rem_time", "int"),
    258: ("bms_min_cell_temp", "int"),
    259: ("bms_max_cell_temp", "int"),
    262: ("cms_batt_soc", "float"),
    269: ("cms_chg_rem_time", "int"),
    281: ("bms_chg_dsg_state", "int"),
    282: ("cms_chg_dsg_state", "int"),
    368: ("pow_get_ac_out", "float"),
}

def _read_varint(buf: bytes, pos: int):
    val, shift = 0, 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
    raise ValueError("Truncated varint")

def _parse_wire(buf: bytes) -> list:
    pos, out = 0, []
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        field_num, wire_type = tag >> 3, tag & 0x07
        if wire_type == 0:
            val, pos = _read_varint(buf, pos)
            out.append((field_num, "varint", val))
        elif wire_type == 1:
            out.append((field_num, "fixed64", buf[pos:pos + 8]))
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            out.append((field_num, "bytes", buf[pos:pos + length]))
            pos += length
        elif wire_type == 5:
            out.append((field_num, "fixed32", buf[pos:pos + 4]))
            pos += 4
        else:
            break
    return out

def decode_ecoflow_protobuf(payload: bytes) -> dict:
    outer = _parse_wire(payload)
    header_bytes = next((v for k, t, v in outer if k == 1 and t == "bytes"), None)
    if not header_bytes:
        return {}

    header = {k: v for k, _, v in _parse_wire(header_bytes)}
    pdata = header.get(1, b"")
    enc_type = header.get(6, 0)
    cmd_func = header.get(8, 0)
    cmd_id = header.get(9, 0)
    seq = header.get(14, 0)

    if enc_type == 1 and isinstance(pdata, bytes):
        xor_key = seq & 0xFF
        pdata = bytes(b ^ xor_key for b in pdata)

    metrics = {}
    if cmd_func == 254 and cmd_id == 21 and isinstance(pdata, bytes):
        for fnum, wtype, val in _parse_wire(pdata):
            if fnum not in RIVER3_FIELDS:
                continue
            name, _ = RIVER3_FIELDS[fnum]
            if wtype == "fixed32":
                num = round(struct.unpack("<f", val)[0], 2)
                if name == "pow_get_ac_out":
                    num = abs(num)
                metrics[name] = num
            elif wtype == "varint":
                metrics[name] = val
    return metrics

# ==========================================
# MQTT BRIDGE (auto re-auth on credential expiry)
# ==========================================
class EcoFlowMqttBridge:
    """
    Owns the MQTT client lifecycle. While connected, paho handles transient
    network drops with its built-in reconnect backoff. If the broker *rejects*
    us (CONNACK rc 4 "bad username/password" or rc 5 "not authorized" -- which
    is what expired MQTT certificates produce), this bridge tears the client
    down, performs a fresh EcoFlow login to obtain a new certificate, and
    reconnects.
    """

    def __init__(self):
        self.creds_stale = threading.Event()
        self.connect_failures = 0

    # ---------- callbacks ----------
    def on_connect(self, client, userdata, flags, reason_code, properties):
        rc = int(getattr(reason_code, "value", reason_code))
        if rc == 0:
            self.connect_failures = 0
            print("[SUCCESS] Connected to EcoFlow MQTT Broker!", flush=True)
            user_id = userdata["userId"]
            for sn in UPS_MAPPING:
                client.subscribe(f"/app/device/property/{sn}")
                client.subscribe(f"/app/{user_id}/{sn}/thing/property")
            return

        self.connect_failures += 1
        print(f"[ERROR] MQTT connection refused (rc={rc}, rejection {self.connect_failures}/{AUTH_FAIL_LIMIT})", flush=True)
        if rc in (4, 5) or self.connect_failures >= AUTH_FAIL_LIMIT:
            print("[INFO] Credentials appear expired - scheduling re-authentication.", flush=True)
            self.creds_stale.set()

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        print(f"[INFO] MQTT disconnected ({reason_code}); paho will auto-reconnect with backoff.", flush=True)

    def on_message(self, client, userdata, msg):
        # Never let a processing error kill the paho network thread --
        # an unhandled exception here silently ends the loop.
        try:
            self._handle_message(msg)
        except Exception as err:
            print(f"[ERROR] Failed to process MQTT message: {err}", flush=True)

    def _handle_message(self, msg):
        parts = msg.topic.split("/")
        sn = parts[-1] if msg.topic.startswith("/app/device/property/") else (parts[3] if len(parts) > 3 else None)
        if sn not in UPS_MAPPING:
            return

        metrics = decode_ecoflow_protobuf(msg.payload)
        if not metrics:
            return

        state = DEVICE_STATES[sn]
        state.update(metrics)

        cfg = UPS_MAPPING[sn]
        nut_name = cfg["nut_name"]
        max_watts = cfg["max_watts"]

        ac_in_w = state.get("pow_get_ac_in", 0.0)
        ac_out_w = state.get("pow_get_ac_out", state.get("pow_out_sum_w", 0.0))
        dsg_state = state.get("cms_chg_dsg_state", state.get("bms_chg_dsg_state", 0))
        soc = round(state.get("cms_batt_soc", state.get("bms_batt_soc", 100.0)), 1)
        rem_mins = state.get("cms_chg_rem_time", state.get("bms_chg_rem_time", 0))
        out_volts = state.get("ac_out_volts", 230)
        freq_hz = state.get("ac_freq_hz", 50)
        temp_c = state.get("bms_max_cell_temp", 25)

        # Determine standard NUT ups.status flags:
        # OL = Online (mains power present)
        # OB = On Battery (power outage)
        # LB = Low Battery (triggers NUT upsmon shutdown)
        # CHRG = Charging
        is_power_failure = (ac_in_w <= 0.5) and (dsg_state == 1)
        if is_power_failure:
            status_flags = ["OB"]
            if soc <= LOW_BATTERY_PCT:
                status_flags.append("LB")
        else:
            status_flags = ["OL"]
            if dsg_state == 2:
                status_flags.append("CHRG")

        nut_vars = {
            "ups.status": " ".join(status_flags),
            "battery.charge": f"{soc}",
            "battery.charge.low": f"{LOW_BATTERY_PCT}",
            "battery.runtime": f"{int(rem_mins * 60)}",  # NUT expects runtime in seconds
            "battery.temperature": f"{temp_c}",
            "input.voltage": f"{out_volts if ac_in_w > 0.5 else 0.0}",
            "input.frequency": f"{freq_hz if ac_in_w > 0.5 else 0}",
            "output.voltage": f"{out_volts}",
            "output.frequency": f"{freq_hz}",
            "ups.realpower": f"{round(ac_out_w, 1)}",
            "ups.load": f"{min(100, round((ac_out_w / max_watts) * 100))}",
        }

        dev_writer.update_vars(nut_vars)
        print(f"[{nut_name}] status={nut_vars['ups.status']} | batt={soc}% | runtime={rem_mins}m | out={ac_out_w}W", flush=True)

    # ---------- client lifecycle ----------
    def _build_client(self, creds: dict) -> mqtt.Client:
        device_uuid = get_persistent_device_uuid()
        client_id = f"ANDROID_{device_uuid}_{creds['userId']}"
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            userdata=creds,
        )
        client.username_pw_set(creds["certificateAccount"], creds["certificatePassword"])
        client.tls_set_context(ssl.create_default_context())
        client.reconnect_delay_set(min_delay=15, max_delay=120)
        client.on_connect = self.on_connect
        client.on_disconnect = self.on_disconnect
        client.on_message = self.on_message
        return client

    def _fresh_credentials(self) -> dict:
        while True:
            try:
                print("[INFO] Authenticating with EcoFlow as Android device...", flush=True)
                creds = get_app_mqtt_credentials()
                print("[INFO] Obtained fresh MQTT credentials.", flush=True)
                return creds
            except Exception as err:
                print(f"[WARN] EcoFlow login failed: {err} - retrying in {REAUTH_RETRY_DELAY}s", flush=True)
                time.sleep(REAUTH_RETRY_DELAY)

    def _teardown(self, client: mqtt.Client):
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass

    def run(self):
        while True:
            creds = self._fresh_credentials()
            self.creds_stale.clear()
            self.connect_failures = 0

            client = self._build_client(creds)

            connected = False
            for attempt in range(1, MAX_CONNECT_ATTEMPTS + 1):
                try:
                    client.connect(creds["url"], int(creds["port"]), keepalive=60)
                    connected = True
                    break
                except Exception as err:
                    print(f"[WARN] Broker connect failed ({err}), attempt {attempt}/{MAX_CONNECT_ATTEMPTS}", flush=True)
                    if attempt < MAX_CONNECT_ATTEMPTS:
                        time.sleep(NETWORK_RETRY_DELAY)

            if not connected:
                print("[INFO] Could not reach broker - refreshing credentials.", flush=True)
                self._teardown(client)
                continue

            print("[INFO] MQTT loop running; watching for credential expiry.", flush=True)
            client.loop_start()

            try:
                while not self.creds_stale.wait(timeout=1.0):
                    pass
            finally:
                print("[INFO] Stopping MQTT client and re-authenticating with new certificate...", flush=True)
                self._teardown(client)

# ==========================================
# MAIN
# ==========================================
def main():
    print(f"[INFO] EcoFlow->NUT bridge | device={DEVICE_SN} | ups={NUT_UPS_NAME} | dev file={NUT_DEV_FILE} | LB at {LOW_BATTERY_PCT}%", flush=True)

    bridge = EcoFlowMqttBridge()
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down...", flush=True)

if __name__ == "__main__":
    main()
