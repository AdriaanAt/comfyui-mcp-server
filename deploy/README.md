# Home automation core - setup runbook

Brings up Mosquitto, Zigbee2MQTT and Home Assistant on **Windows 11 + Docker
Desktop (WSL2 backend)**, and pairs the Sonoff Zigbee water valve and door
sensors.

Everything here is free and open source.

---

## Why WSL2 actually helps here

Zigbee2MQTT on **native Windows** with a CP2102N-based dongle hits a known bug:
endless `Waiting for RSTACK` reset loops ending in `HOST_FATAL_ERROR`
([#31281](https://github.com/Koenkk/zigbee2mqtt/issues/31281), dup of
[#28743](https://github.com/Koenkk/zigbee2mqtt/issues/28743)). The root cause is
*Windows* serial-port handling, not the dongle and not the firmware - reflashing
does not fix it.

Under WSL2 the dongle is forwarded as a raw USB device and the **Linux** `cp210x`
driver takes over, so that Windows bug does not apply. The trade-off is the USB
attachment step below, which does not survive a reboot or a replug.

---

## 1. Forward the dongle into WSL2

Install [usbipd-win](https://github.com/dorssel/usbipd-win), then in an
**Administrator** PowerShell:

```powershell
usbipd list                      # find the SONOFF dongle, note its BUSID
usbipd bind   --busid <BUSID>    # once per dongle, persists
usbipd attach --wsl --busid <BUSID>
```

In WSL2, confirm the kernel picked it up:

```bash
lsusb                    # the dongle should appear
sudo modprobe cp210x     # recent WSL2 kernels ship this as a module
ls -l /dev/ttyUSB*       # expect /dev/ttyUSB0
```

Make the module load every boot:

```bash
echo cp210x | sudo tee -a /etc/modules
```

**If `/dev/ttyUSB0` never appears**, your WSL2 kernel lacks the CP210x module and
must be rebuilt with `CONFIG_USB_SERIAL_CP210X` plus USB-IP support. See
[rohzb/wsl2-usb-devices](https://github.com/rohzb/wsl2-usb-devices), which also
scripts re-attachment at boot.

> `usbipd attach` must be repeated after every Windows reboot or replug. This is
> the fragile part of running the core on the main PC — worth automating, or
> worth moving the core to a small always-on Linux box later.

---

## 2. Configure

```bash
cp deploy/.env.example deploy/.env
# edit deploy/.env - set MQTT_PASSWORD to something real
```

Create the broker password file (same user/pass as `.env`):

```bash
docker run --rm -it -v "$PWD/deploy/mosquitto/config:/mosquitto/config" \
  eclipse-mosquitto:2 mosquitto_passwd -c -b /mosquitto/config/passwd homecore 'YOUR_PASSWORD'
```

> **Windows/Docker Desktop gotcha:** `mosquitto_passwd -c` creates `passwd` as
> `0600`, root-owned. The container drops privileges to a non-root user before
> reading it, so the broker fails to start with `Error: Unable to open pwfile`
> and restart-loops. `chmod` from the Windows side does not reliably change
> what the container sees on a bind-mounted drive; fix it from inside a Linux
> container instead:
>
> ```bash
> docker run --rm -v "$PWD/deploy/mosquitto/config:/mosquitto/config" \
>   --entrypoint sh eclipse-mosquitto:2 -c "chmod 644 /mosquitto/config/passwd"
> ```

---

## 3. Start

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml logs -f zigbee2mqtt
```

A healthy start logs `Coordinator firmware version` and `Zigbee2MQTT started`.
`ASH_ERROR_TIMEOUT` or a reset loop means the serial device is wrong or the
`ember` adapter did not take — recheck `serial.port` in
`deploy/zigbee2mqtt/configuration.yaml`.

Then:

| Service | URL |
|---|---|
| Home Assistant | http://localhost:8123 |
| Zigbee2MQTT | http://localhost:8080 |
| MQTT | `localhost:1883` |

Verify from the repo root:

```bash
python scripts/stack_check.py --only mosquitto zigbee home
```

---

## 4. Pair the water valve

1. Zigbee2MQTT frontend → **Permit join**, scoped to the coordinator, 5 minutes.
2. Factory-reset the valve (hold its button until the LED flashes).
3. It appears in the device list; rename it to `water_valve`.
4. Close permit-join again.

Confirm it publishes:

```bash
docker exec -it mosquitto mosquitto_sub -u homecore -P 'YOUR_PASSWORD' \
  -t 'zigbee2mqtt/#' -v
```

Door and window sensors pair the same way.

---

## 5. Connect Home Assistant

MQTT discovery is already on (`homeassistant.enabled: true` in the Z2M config),
so paired devices surface automatically:

**Settings → Devices & Services → Add Integration → MQTT**
Broker `mosquitto`, port `1883`, and the `.env` credentials.

Then add **Model Context Protocol Server** the same way. That exposes the Assist
API at `/api/mcp` so Open WebUI agents can control the house — and
**Settings → Voice assistants → Exposed entities** controls exactly which
devices they may touch. Keep that list tight: expose the valve and the lights,
not the alarm.

---

## Next

- Irrigation schedule + door-open alert automations.
- Wi-Fi devices (4CHR3, MTS22) onto MQTT via Tasmota/ESPHome.
- Cameras: enable ONVIF/RTSP in eWeLink (Device Settings → More Settings →
  ONVIF/RTSP), then add via go2rtc/Frigate.
- Back up `deploy/zigbee2mqtt/configuration.yaml` once devices are joined —
  losing the network key means re-pairing everything.
