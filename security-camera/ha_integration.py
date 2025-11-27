#!/usr/bin/env python3
"""
Home Assistant integration for motion detection sensors.
Exposes motion state and recording info via JSON state file.

Home Assistant can read the state file using command_line or file sensors,
or use the HTTP API endpoint.
"""

import json
import time
import threading
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class SensorState:
    """State of all exposed sensors."""
    motion_detected: bool
    motion_state: str  # idle, detecting, active
    last_motion_time: Optional[str]
    is_recording: bool
    total_recordings: int
    latest_recording: Optional[str]
    latest_recording_time: Optional[str]
    latest_thumbnail: Optional[str]
    uptime_seconds: int
    frames_processed: int
    motion_events_today: int

    def to_dict(self) -> dict:
        return asdict(self)


class HAIntegration:
    """
    Manages Home Assistant integration by writing sensor state to a JSON file.

    The state file can be read by:
    1. HA command_line sensor (cat /share/security_state.json | jq .motion_detected)
    2. HA file sensor
    3. HTTP API endpoint served by http_server.py
    """

    def __init__(
        self,
        state_file: str = "/share/security_state.json",
        update_interval: float = 1.0
    ):
        """
        Initialize HA integration.

        Args:
            state_file: Path to write sensor state JSON
            update_interval: How often to write state (seconds)
        """
        self.state_file = Path(state_file)
        self.update_interval = update_interval

        # State tracking
        self.motion_detected = False
        self.motion_state = "idle"
        self.last_motion_time: Optional[float] = None
        self.is_recording = False
        self.total_recordings = 0
        self.latest_recording: Optional[str] = None
        self.latest_recording_time: Optional[float] = None
        self.latest_thumbnail: Optional[str] = None

        # Stats
        self.start_time = time.time()
        self.frames_processed = 0
        self.motion_events_today = 0
        self._last_event_date: Optional[str] = None

        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Ensure parent directory exists
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def update_motion_state(
        self,
        detected: bool,
        state: str,
        frames_processed: int = 0
    ):
        """Update motion detection state."""
        with self._lock:
            self.motion_detected = detected
            self.motion_state = state
            self.frames_processed = frames_processed

            if detected:
                self.last_motion_time = time.time()

                # Track daily events
                today = datetime.now().strftime("%Y-%m-%d")
                if self._last_event_date != today:
                    self.motion_events_today = 0
                    self._last_event_date = today

                # Only increment on state transition to active
                if state == "active" and not self.motion_detected:
                    self.motion_events_today += 1

    def update_recording_state(
        self,
        is_recording: bool,
        total_recordings: int = 0,
        latest_recording: Optional[str] = None,
        latest_recording_time: Optional[float] = None,
        latest_thumbnail: Optional[str] = None
    ):
        """Update recording state."""
        with self._lock:
            self.is_recording = is_recording
            self.total_recordings = total_recordings
            if latest_recording:
                self.latest_recording = latest_recording
                self.latest_recording_time = latest_recording_time
                self.latest_thumbnail = latest_thumbnail

    def _format_timestamp(self, ts: Optional[float]) -> Optional[str]:
        """Format Unix timestamp as ISO string."""
        if ts is None:
            return None
        return datetime.fromtimestamp(ts).isoformat()

    def get_state(self) -> SensorState:
        """Get current sensor state."""
        with self._lock:
            return SensorState(
                motion_detected=self.motion_detected,
                motion_state=self.motion_state,
                last_motion_time=self._format_timestamp(self.last_motion_time),
                is_recording=self.is_recording,
                total_recordings=self.total_recordings,
                latest_recording=self.latest_recording,
                latest_recording_time=self._format_timestamp(self.latest_recording_time),
                latest_thumbnail=self.latest_thumbnail,
                uptime_seconds=int(time.time() - self.start_time),
                frames_processed=self.frames_processed,
                motion_events_today=self.motion_events_today
            )

    def _write_state(self):
        """Write current state to JSON file."""
        state = self.get_state()
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state.to_dict(), f, indent=2)
        except Exception as e:
            logger.error(f"Error writing state file: {e}")

    def _update_loop(self):
        """Periodically write state to file."""
        logger.info(f"Starting HA integration, writing to {self.state_file}")

        while self._running:
            self._write_state()
            time.sleep(self.update_interval)

        # Final write on shutdown
        self._write_state()
        logger.info("HA integration stopped")

    def start(self):
        """Start background state writing."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._update_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop background state writing."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


def generate_ha_config_example() -> str:
    """Generate example Home Assistant configuration."""
    return """
# Example Home Assistant configuration for security-camera add-on
# Add this to your configuration.yaml

# Command line sensors (read from JSON state file)
command_line:
  - sensor:
      name: "Security Camera Motion"
      command: "cat /share/security_state.json | jq -r '.motion_detected'"
      scan_interval: 2
      value_template: "{{ value }}"

  - sensor:
      name: "Security Camera State"
      command: "cat /share/security_state.json | jq -r '.motion_state'"
      scan_interval: 2

  - sensor:
      name: "Security Camera Recordings"
      command: "cat /share/security_state.json | jq -r '.total_recordings'"
      scan_interval: 60

# Template binary sensor for easier automation
template:
  - binary_sensor:
      - name: "Security Camera Motion Detected"
        state: "{{ states('sensor.security_camera_motion') == 'true' }}"
        device_class: motion

# Example automation
automation:
  - alias: "Security Camera Motion Alert"
    trigger:
      - platform: state
        entity_id: binary_sensor.security_camera_motion_detected
        to: "on"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Motion Detected"
          message: "Motion detected at front door"
          data:
            image: "/local/security_recordings/{{ states('sensor.security_camera_latest_thumbnail') }}"

# REST sensor alternative (if HTTP server is accessible)
rest:
  - resource: http://localhost:8081/api/state
    scan_interval: 2
    sensor:
      - name: "Security Motion API"
        value_template: "{{ value_json.motion_detected }}"
"""


# For standalone testing
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    integration = HAIntegration(
        state_file="./test_state.json",
        update_interval=1.0
    )

    integration.start()

    # Simulate some state changes
    print("Simulating motion detection...")

    integration.update_motion_state(False, "idle", 10)
    time.sleep(2)

    integration.update_motion_state(True, "detecting", 20)
    time.sleep(2)

    integration.update_motion_state(True, "active", 30)
    integration.update_recording_state(True, 5, "motion_20241127_120000.mp4", time.time())
    time.sleep(2)

    integration.update_motion_state(False, "idle", 40)
    integration.update_recording_state(False, 6, "motion_20241127_120000.mp4", time.time(), "motion_20241127_120000.jpg")
    time.sleep(2)

    integration.stop()

    # Print state file
    with open("./test_state.json", 'r') as f:
        print(f.read())

    # Print HA config example
    print("\n" + "="*60)
    print("Example Home Assistant configuration:")
    print("="*60)
    print(generate_ha_config_example())
