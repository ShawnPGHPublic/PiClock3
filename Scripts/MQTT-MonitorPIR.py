#!/usr/bin/env python3
"""MQTT monitor power + PIR motion notifications (Raspberry Pi)."""

import logging
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
import os

# --- MQTT ---
MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_USERNAME = None
MQTT_PASSWORD = None
MQTT_TOPIC = "piclock/monitor"
MQTT_STATE_TOPIC = "piclock/monitor/state"
MQTT_CLIENT_ID = "piclock"
MQTT_MOTION_TOPIC = "piclock/motion"

# --- Display ---
OUTPUT_NAME = "HDMI-A-1"
#OUTPUT_MODE = "1280x1024@60.02Hz"
#OUTPUT_TRANSFORM = "normal"

# --- WWZMDiB PIR (HC-SR501 compatible), BCM numbering ---
PIR_PIN = 17                 # OUT pin of the PIR, e.g. GPIO17 physical pin 11
PIR_PULL_UP = False          # most HC-SR501 modules are active-high, no pull-up
PIR_WARMUP_SEC = 45
PIR_QUEUE_MAX = 50

PIR_SAMPLE_RATE = 10
PIR_QUEUE = 30
PIR_THRESHOLD = 0.95

MONITOR_CHECK_SECONDS = 1 * 60

ON_PAYLOADS = {"on", "1", "true", "enable"}
OFF_PAYLOADS = {"off", "0", "false", "disable"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("monitor-mqtt")

motion_events: queue.Queue[str] = queue.Queue(maxsize=PIR_QUEUE_MAX)

global curMonitorState

os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")

def monitor_is_on() -> bool | None:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("WAYLAND_DISPLAY", os.environ.get("WAYLAND_DISPLAY", "wayland-0"))

    try:
        r = subprocess.run(
            ["wlopm"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        log.warning("Could not query wlopm: %s", e)
        return None

    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[-1].lower()
        if name != OUTPUT_NAME:
            continue
        if state in {"on", "yes", "true"}:
            return True
        if state in {"off", "no", "false"}:
            return False
        log.warning("Unexpected wlopm state for %s: %r", OUTPUT_NAME, line)
        return None

    log.warning("Could not parse wlopm status for %s", OUTPUT_NAME)
    return None

def set_monitor(on: bool) -> bool:
    action = "--on" if on else "--off"
    # cmd = ["wlr-randr", "--output", OUTPUT_NAME, action,"--mode",OUTPUT_MODE,"--transform",OUTPUT_TRANSFORM]    
    cmd = ["wlopm",action,OUTPUT_NAME]    
    log.info("Running: %s", " ".join(cmd))
    
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, text=True)
        if r.stdout.strip():
            log.info("%s", r.stdout.strip())
        if r.stderr.strip():
            log.warning("%s", r.stderr.strip())
        log.info("Monitor %s", "ON" if on else "OFF")
        return True
    except FileNotFoundError:
        log.error("wlopm not found on PATH")
    except subprocess.CalledProcessError as e:
        log.error("wlopm failed (%s): %s", e.returncode, e.stderr)
    return False


def start_pir() -> None:
    try:
        from gpiozero import MotionSensor
    except ImportError:
        log.error("gpiozero missing: sudo apt install python3-gpiozero")
        return

    def put(payload: str) -> None:
        try:
            motion_events.put_nowait(payload)
        except queue.Full:
            log.warning("Motion queue full; dropping %s", payload)

    log.info("WWZMDiB PIR warmup %ss (ignore motion during this)", PIR_WARMUP_SEC)
    time.sleep(PIR_WARMUP_SEC)

    # queue_len=1: report the raw HC-SR501 edge, don't average it
    pir = MotionSensor(
        PIR_PIN,
        pull_up=False,
        queue_len=PIR_QUEUE,
        sample_rate=PIR_SAMPLE_RATE,
        threshold=PIR_THRESHOLD
    )
    pir.when_motion = lambda: put("ON")
    pir.when_no_motion = lambda: put("OFF")
    log.info("WWZMDiB PIR ready on BCM GPIO %s", PIR_PIN)
    threading.Event().wait()


def _enc_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _encode_remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n > 0:
            byte |= 0x80
        out.append(byte)
        if n == 0:
            break
    return bytes(out)


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("broker closed connection")
        buf.extend(chunk)
    return bytes(buf)


def _read_packet(sock: socket.socket):
    header = _read_exact(sock, 1)[0]
    multiplier = 1
    remaining = 0
    while True:
        byte = _read_exact(sock, 1)[0]
        remaining += (byte & 0x7F) * multiplier
        if byte & 0x80 == 0:
            break
        multiplier *= 128
        if multiplier > 128 * 128 * 128:
            raise ValueError("bad remaining length")
    payload = _read_exact(sock, remaining) if remaining else b""
    return header, payload


def mqtt_publish(sock: socket.socket, topic: str, payload: str, retain: bool = False) -> None:
    flags = 0x30
    if retain:
        flags |= 0x01
    body = _enc_str(topic) + payload.encode("utf-8")
    sock.sendall(bytes([flags]) + _encode_remaining_length(len(body)) + body)
    log.info("Published %s -> %s%s", topic, payload, " (retain)" if retain else "")


def publish_state(sock: socket.socket, on: bool) -> None:
    mqtt_publish(sock, MQTT_STATE_TOPIC, "ON" if on else "OFF", retain=True)


def publish_actual_state(sock: socket.socket) -> None:
    global curMonitorState
    if curMonitorState is None:
        log.warning("Skipping monitor state publish")
        return
    publish_state(sock, curMonitorState)


def drain_motion(sock: socket.socket) -> None:
    while True:
        try:
            payload = motion_events.get_nowait()
        except queue.Empty:
            return
        mqtt_publish(sock, MQTT_MOTION_TOPIC, payload, retain=True)


def mqtt_connect(sock: socket.socket) -> None:
    proto = _enc_str("MQTT") + b"\x04"
    flags = 0x02
    if MQTT_USERNAME:
        flags |= 0x80
        if MQTT_PASSWORD:
            flags |= 0x40
    variable = proto + bytes([flags]) + struct.pack("!H", 60) + _enc_str(MQTT_CLIENT_ID)
    if MQTT_USERNAME:
        variable += _enc_str(MQTT_USERNAME)
        if MQTT_PASSWORD:
            variable += _enc_str(MQTT_PASSWORD)
    sock.sendall(bytes([0x10]) + _encode_remaining_length(len(variable)) + variable)

    header, payload = _read_packet(sock)
    if (header & 0xF0) != 0x20 or len(payload) < 2 or payload[1] != 0:
        raise ConnectionError(f"CONNACK failed: header={header:#x} payload={payload!r}")
    log.info("Connected to %s:%s", MQTT_HOST, MQTT_PORT)


def mqtt_subscribe(sock: socket.socket) -> None:
    variable = struct.pack("!H", 1) + _enc_str(MQTT_TOPIC) + b"\x00"
    sock.sendall(bytes([0x82]) + _encode_remaining_length(len(variable)) + variable)
    header, payload = _read_packet(sock)
    if (header & 0xF0) != 0x90:
        raise ConnectionError(f"expected SUBACK, got header={header:#x}")
    log.info("Subscribed to %s", MQTT_TOPIC)


def handle_publish(sock: socket.socket, header: int, payload: bytes) -> None:
    global curMonitorState
    
    qos = (header >> 1) & 0x03
    if len(payload) < 2:
        return
    tlen = struct.unpack("!H", payload[:2])[0]
    topic = payload[2:2 + tlen].decode("utf-8", errors="replace")
    rest = payload[2 + tlen:]
    if qos > 0:
        rest = rest[2:]
    msg = rest.decode("utf-8", errors="replace").strip().lower()
    log.info("Message on %s: %r", topic, msg)

    if topic != MQTT_TOPIC:
        return
    if msg in ON_PAYLOADS:
        if set_monitor(True):
            curMonitorState = True
            publish_state(sock, True)
    elif msg in OFF_PAYLOADS:
        if set_monitor(False):
            curMonitorState = False
            publish_state(sock, False)
    else:
        log.warning("Unknown payload %r", msg)


def ping(sock: socket.socket) -> None:
    sock.sendall(b"\xc0\x00")


def session() -> None:
    with socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=30) as sock:
        sock.settimeout(0.5)
        mqtt_connect(sock)
        mqtt_subscribe(sock)

        global curMonitorState
        curMonitorState = monitor_is_on()
        publish_actual_state(sock)

        log.info("Initial Monitor State: %s",curMonitorState)


        stop = threading.Event()
        monitor_thread = threading.Thread(
            target=_publish_monitorstate_loop,
            args=(sock, stop),
            name="monitor-state",
            daemon=True,
        )
        monitor_thread.start()

        last_ping = time.monotonic()
        try:
            while True:
                drain_motion(sock)
                try:
                    header, payload = _read_packet(sock)
                except socket.timeout:
                    if time.monotonic() - last_ping > 50:
                        ping(sock)
                        last_ping = time.monotonic()
                    continue
                ptype = header & 0xF0
                if ptype == 0x30:
                    handle_publish(sock, header, payload)
                elif ptype == 0xD0:
                    pass
                elif ptype == 0xE0:
                    raise ConnectionError("broker sent DISCONNECT")
                if time.monotonic() - last_ping > 50:
                    ping(sock)
                    last_ping = time.monotonic()
        finally:
            stop.set()
            monitor_thread.join(timeout=2)


def _publish_monitorstate_loop(sock: socket.socket, stop: threading.Event) -> None:
    while not stop.wait(MONITOR_CHECK_SECONDS):
        try:
            global curMonitorState
            curState = monitor_is_on();
            #log.info("Checking monitor state %s / %s",curState,curMonitorState)
            if curState != curMonitorState:            
                curMonitorState = curState
                publish_actual_state(sock)
            
        except Exception:
            log.exception("Periodic monitor state publish failed")

def main() -> int:
    pir_thread = threading.Thread(target=start_pir, name="pir", daemon=True)
    pir_thread.start()

    while True:
        try:
            log.info("Connecting...")
            session()
        except KeyboardInterrupt:
            log.info("Stopping")
            return 0
        except Exception as e:
            log.error("%s — retry in 5s", e)
            time.sleep(5)

if __name__ == "__main__":
    sys.exit(main())
