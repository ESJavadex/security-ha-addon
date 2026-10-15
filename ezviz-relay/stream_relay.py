#!/usr/bin/env python3
"""
EZVIZ -> HLS relay without the 30 s gaps.

EZVIZ closes every VTDU stream session after ~30 s and rejects keepalives.
The old loop (stream_to_pipe.py | ffmpeg, restarted each time) refreshed the
login, listed devices, asked for tokens, reconnected and re-probed the video
on every cut: ~8 s of footage lost every 30 s.

This relay keeps one login and one ffmpeg alive and opens the next VTDU
session *before* the current one expires. As soon as the new session delivers
video, output switches to it and the old one is closed, so the byte stream
that reaches ffmpeg has no holes.

Environment:
    EZVIZ_EMAIL, EZVIZ_PASSWORD, EZVIZ_SERIAL, EZVIZ_REGION (default Europe)
    EZVIZ_MFA_CODE            only while registering this terminal
    EZVIZ_TOKEN_FILE          session cache (see ezviz_stream.py)
    EZVIZ_FEATURE_CODE_FILE   terminal id (see ezviz_stream.py)
    HLS_DIR                   output directory (default /var/lib/seccam/hls)
    HLS_TIME, HLS_LIST_SIZE   default 2 / 10
    HANDOVER_AFTER            seconds into a session to open the next one (default 20)
"""

import logging
import os
import queue
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

from ezviz_stream import EzvizAuthError, EzvizCamera, EzvizMFARequired

log = logging.getLogger("relay")

EXIT_AUTH = 78
PS_PACK_START = b"\x00\x00\x01\xba"


class SessionError(Exception):
    pass


class StreamSession:
    """One VTDU stream session; a reader thread queues its video payloads."""

    _ids = 0

    def __init__(self, relay):
        StreamSession._ids += 1
        self.id = StreamSession._ids
        self.relay = relay
        self.sock = None
        self.queue = queue.Queue(maxsize=2000)
        self.opened_at = None
        self.first_data_at = None
        self.closed = threading.Event()
        self.ended_at = None
        self.bytes = 0
        self._thread = None

    def open(self):
        """Connect VTM -> VTDU and request the stream. Raises SessionError."""
        t0 = time.time()
        vtdu_ip, vtdu_port, vtdu_url = self.relay.stream_endpoint()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            sock.connect((vtdu_ip, vtdu_port))
            sock.sendall(self.relay.camera._create_stream_request(vtdu_url))
            header = self._recv_exact(sock, 8)
            _, _, length, _, _ = self.relay.camera._parse_header(header)
            if length:
                self._recv_exact(sock, length)
        except Exception as e:
            sock.close()
            raise SessionError(f"VTDU {vtdu_ip}:{vtdu_port}: {e}") from e
        self.sock = sock
        self.opened_at = time.time()
        log.info(f"session {self.id} open in {self.opened_at - t0:.1f}s ({vtdu_ip}:{vtdu_port})")
        self._thread = threading.Thread(target=self._read, daemon=True, name=f"session-{self.id}")
        self._thread.start()

    @staticmethod
    def _recv_exact(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _read(self):
        parse = self.relay.camera._parse_header
        try:
            while not self.closed.is_set():
                header = self._recv_exact(self.sock, 8)
                try:
                    _, channel, length, _, _ = parse(header)
                except ValueError:
                    continue
                data = self._recv_exact(self.sock, length) if length else b""
                if channel == 0x01 and data:
                    if self.first_data_at is None:
                        self.first_data_at = time.time()
                    self.bytes += len(data)
                    try:
                        self.queue.put(data, timeout=5)
                    except queue.Full:
                        log.warning(f"session {self.id}: output stalled, dropping data")
        except Exception as e:
            if not self.closed.is_set():
                log.info(f"session {self.id} ended after {self.age():.1f}s ({e})")
        finally:
            self.ended_at = time.time()
            self.closed.set()
            self.queue.put(None)  # end marker

    def age(self):
        return (time.time() - self.opened_at) if self.opened_at else 0.0

    def close(self):
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class Relay:
    def __init__(self):
        self.email = os.environ["EZVIZ_EMAIL"]
        self.password = os.environ["EZVIZ_PASSWORD"]
        self.serial = os.environ["EZVIZ_SERIAL"]
        self.region = os.environ.get("EZVIZ_REGION", "Europe")
        self.mfa_code = os.environ.get("EZVIZ_MFA_CODE") or None
        self.hls_dir = os.environ.get("HLS_DIR", "/var/lib/seccam/hls")
        self.hls_time = int(os.environ.get("HLS_TIME", 2))
        self.hls_list_size = int(os.environ.get("HLS_LIST_SIZE", 10))
        self.handover_after = float(os.environ.get("HANDOVER_AFTER", 20))
        self.camera = None
        self._devices = None
        self._devices_at = 0.0
        self._tokens = []
        self.ffmpeg = None
        self._raw_dump = open(os.environ["RAW_DUMP"], "wb") if os.environ.get("RAW_DUMP") else None
        self._stop = threading.Event()
        self.lifetimes = []
        self.handovers = 0
        self.gaps = 0

    # ── EZVIZ API ──────────────────────────────────────────────────────

    def login(self):
        self.camera = EzvizCamera(self.email, self.password, self.serial, self.region,
                                  mfa_code=self.mfa_code)
        self.camera.connect()
        self._devices = None
        self._tokens = []
        log.info("logged in to EZVIZ")

    def stream_endpoint(self):
        """VTM redirect -> (vtdu_ip, vtdu_port, vtdu_url). Device list and tokens are cached."""
        cam = self.camera
        if self._devices is None or time.time() - self._devices_at > 600:
            devices = cam.api.get_devices()
            if not devices:
                raise SessionError("device list failed")
            self._devices, self._devices_at = devices, time.time()
        resource = next((r for r in self._devices.get("resourceInfos", [])
                         if r["deviceSerial"] == self.serial and r.get("resourceType", 0) > 0), None)
        if not resource:
            raise SessionError(f"device {self.serial} not found")
        vtm = self._devices["VTM"].get(resource["resourceId"])
        if not vtm:
            raise SessionError("VTM info not found")
        if vtm != getattr(self, "_last_vtm", None):
            self._last_vtm = vtm
            log.info(f"VTM {vtm['externalIp']}:{vtm['port']}")
        if not self._tokens:
            data = cam.api.get_vtdu_tokens()
            if not data or not data.get("tokens"):
                raise SessionError("no VTDU tokens")
            self._tokens = list(data["tokens"])
        token = self._tokens.pop(0)
        url = cam.stream._build_stream_url(vtm["externalIp"], vtm["port"], self.serial, token)
        vtm_sock = cam.stream._connect_socket(vtm["externalIp"], vtm["port"])
        try:
            _, body = cam.stream._send_stream_request(vtm_sock, url)
        finally:
            vtm_sock.close()
        text = body.decode("utf-8", errors="ignore")
        start = text.find("ysproto://")
        if start < 0:
            self._tokens = []  # token probably rejected; fetch fresh ones next time
            raise SessionError("VTM did not return a VTDU address")
        vtdu_url = text[start:].split("\x00")[0]
        m = re.search(r"ysproto://([^:]+):(\d+)/", vtdu_url)
        return m.group(1), int(m.group(2)), vtdu_url

    def open_session(self, attempts=4):
        delay = 0.5
        for i in range(attempts):
            s = StreamSession(self)
            try:
                s.open()
                return s
            except SessionError as e:
                log.warning(f"opening session failed ({e}), attempt {i + 1}/{attempts}")
            except Exception as e:
                log.warning(f"opening session failed ({e!r}), attempt {i + 1}/{attempts}")
            time.sleep(delay)
            delay = min(delay * 2, 5)
            if i == attempts // 2:
                # Repeated failures: the session may have expired
                try:
                    self.login()
                except (EzvizMFARequired, EzvizAuthError):
                    raise
                except Exception as e:
                    log.warning(f"re-login failed: {e}")
        raise SessionError("could not open a stream session")

    # ── ffmpeg ─────────────────────────────────────────────────────────

    def start_ffmpeg(self):
        os.makedirs(self.hls_dir, exist_ok=True)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", os.environ.get("FFMPEG_LOGLEVEL", "error"), "-nostdin",
            # The camera's own timestamps are continuous across sessions, but a
            # new session restarts at the previous keyframe (~2 s back). Decode
            # everything and keep only frames that move time forward (a jump back
            # of more than 10 s is a camera clock reset and is accepted).
            # -copyts: otherwise ffmpeg "repairs" the jump back of the mpeg
            # input before the select filter can drop the repeated frames.
            "-copyts", "-start_at_zero",
            "-fflags", "+genpts+discardcorrupt",
            "-probesize", "1M", "-analyzeduration", "1000000",
            "-f", "mpeg", "-i", "pipe:0",
            "-an",
            "-vf", "select='isnan(prev_selected_t)+gt(t,prev_selected_t)+lt(t,prev_selected_t-10)'",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-crf", "23", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
            "-fps_mode", "cfr", "-r", "15",
            "-f", "hls",
            "-hls_time", str(self.hls_time),
            "-hls_list_size", str(self.hls_list_size),
            "-hls_flags", "delete_segments+omit_endlist+independent_segments+temp_file",
            "-hls_start_number_source", "epoch",
            "-hls_segment_filename", os.path.join(self.hls_dir, "seg_%d.ts"),
            os.path.join(self.hls_dir, "stream.m3u8"),
        ]
        self.ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        log.info(f"ffmpeg started (pid {self.ffmpeg.pid})")

    def write(self, data):
        if self._raw_dump:
            self._raw_dump.write(data)
        if self.ffmpeg is None or self.ffmpeg.poll() is not None:
            if self.ffmpeg is not None:
                log.warning(f"ffmpeg exited ({self.ffmpeg.returncode}), restarting")
            self.start_ffmpeg()
        try:
            self.ffmpeg.stdin.write(data)
        except (BrokenPipeError, ValueError):
            log.warning("ffmpeg pipe broken, restarting")
            try:
                self.ffmpeg.kill()
            except Exception:
                pass
            self.ffmpeg = None

    # ── Main loop ──────────────────────────────────────────────────────

    def run(self):
        self.login()
        self.start_ffmpeg()
        current = self.open_session()
        nxt = None
        nxt_thread = None
        last_report = time.time()
        gap_started = None

        def prepare_next():
            nonlocal nxt
            try:
                nxt = self.open_session()
            except SessionError as e:
                log.error(str(e))

        while not self._stop.is_set():
            # Open the next session ahead of the expected cut
            if nxt is None and (nxt_thread is None or not nxt_thread.is_alive()) \
                    and current.age() >= self.handover_after:
                nxt_thread = threading.Thread(target=prepare_next, daemon=True, name="prepare-next")
                nxt_thread.start()

            # Switch as soon as the next session has video
            if nxt is not None and nxt.first_data_at is not None:
                lifetime = current.age()
                self.handovers += 1
                current.close()
                if current.ended_at is None or current.ended_at >= nxt.first_data_at:
                    log.info(f"handover {current.id} -> {nxt.id} after {lifetime:.1f}s, no gap")
                current, nxt = nxt, None
                gap_started = None
                continue

            try:
                data = current.queue.get(timeout=0.2)
            except queue.Empty:
                data = b""
            if data is None:
                # Current session ended (EZVIZ cut it)
                self.lifetimes.append(current.ended_at - current.opened_at)
                if nxt is not None and nxt.first_data_at is not None:
                    continue  # handled above on next iteration
                if gap_started is None:
                    gap_started = time.time()
                    self.gaps += 1
                    log.warning(f"session {current.id} ended before the next one was ready "
                                f"(lived {self.lifetimes[-1]:.1f}s)")
                if nxt is None and (nxt_thread is None or not nxt_thread.is_alive()):
                    nxt_thread = threading.Thread(target=prepare_next, daemon=True, name="prepare-next")
                    nxt_thread.start()
                # Wait for the next session to deliver
                while nxt is None or nxt.first_data_at is None:
                    if self._stop.is_set():
                        return
                    if nxt is None and not nxt_thread.is_alive():
                        nxt_thread = threading.Thread(target=prepare_next, daemon=True, name="prepare-next")
                        nxt_thread.start()
                    time.sleep(0.05)
                log.info(f"resumed with session {nxt.id} after a {time.time() - gap_started:.1f}s gap")
                current, nxt = nxt, None
                gap_started = None
                continue
            if data:
                self.write(data)

            if time.time() - last_report > 600:
                last_report = time.time()
                lt = sorted(self.lifetimes[-20:])
                med = lt[len(lt) // 2] if lt else 0
                log.info(f"stats: {self.handovers} handovers, {self.gaps} gaps, "
                         f"median session {med:.1f}s, handover at {self.handover_after:.0f}s")
                # Keep the handover ~8 s before the typical cut
                if lt and self.gaps and med - 8 < self.handover_after:
                    self.handover_after = max(8.0, med - 8)
                    log.info(f"handover moved to {self.handover_after:.0f}s")

    def stop(self, *_):
        self._stop.set()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    relay = Relay()
    signal.signal(signal.SIGTERM, relay.stop)
    signal.signal(signal.SIGINT, relay.stop)
    try:
        relay.run()
    except EzvizMFARequired as e:
        log.error(f"EZVIZ asks to verify this terminal ({e}). A code was sent to the account; "
                  "set EZVIZ_MFA_CODE once and restart.")
        sys.exit(EXIT_AUTH)
    except EzvizAuthError as e:
        log.error(f"EZVIZ rejected the login: {e}")
        sys.exit(EXIT_AUTH)
    finally:
        if relay.ffmpeg and relay.ffmpeg.poll() is None:
            try:
                relay.ffmpeg.stdin.close()
            except Exception:
                pass
            relay.ffmpeg.terminate()


if __name__ == "__main__":
    main()
