#!/usr/bin/env python3
"""
Motion detection service: detector + recorder + HA state file.

Runs the same way inside the Home Assistant add-on (run.sh) and as a standalone
systemd service (deploy/). All configuration comes from environment variables.
"""

import logging
import os
import signal
import sys
import time

from motion_detector import MotionDetector, MotionEvent
from recording_manager import RecordingManager
from ha_integration import HAIntegration
from llm_analyzer import LLMAnalyzer


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


def main():
    log_level = os.environ.get('LOG_LEVEL', 'info').upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        stream=sys.stderr
    )

    stream_url = os.environ['STREAM_URL']
    motion_threshold = int(os.environ.get('MOTION_THRESHOLD', 5000))
    min_duration = float(os.environ.get('MOTION_MIN_DURATION', 5))
    pre_roll = int(os.environ.get('RECORDING_PRE_ROLL', 10))
    post_roll = int(os.environ.get('RECORDING_POST_ROLL', 0))
    motion_cooldown = float(os.environ.get('MOTION_COOLDOWN', 10))
    recordings_path = os.environ.get('RECORDINGS_PATH', '/share/security_recordings')
    max_recordings = int(os.environ.get('MAX_RECORDINGS', 50))
    state_file = os.environ.get('STATE_FILE', '/share/security_state.json')
    settings_file = os.environ.get('SETTINGS_FILE', '/share/security_settings.json')
    roi_x_start = int(os.environ.get('ROI_X_START', 0))
    roi_x_end = int(os.environ.get('ROI_X_END', 100))
    roi_y_start = int(os.environ.get('ROI_Y_START', 5))
    roi_y_end = int(os.environ.get('ROI_Y_END', 85))
    detect_fps = float(os.environ.get('DETECT_FPS', 4))
    person_model = os.environ.get('PERSON_MODEL', '').strip() or None
    person_score = float(os.environ.get('PERSON_SCORE', 0.5))
    person_input_size = int(os.environ.get('PERSON_INPUT_SIZE', 416))
    motion_fallback = float(os.environ.get('MOTION_FALLBACK', 8))

    llm_enabled = env_bool('LLM_ENABLED')
    llm_auto_analyze = env_bool('LLM_AUTO_ANALYZE')
    llm_api_url = os.environ.get('LLM_API_URL', '')

    llm_analyzer = None
    if llm_enabled and llm_api_url:
        llm_analyzer = LLMAnalyzer(
            api_url=llm_api_url,
            model_name=os.environ.get('LLM_MODEL', 'llava'),
            api_key=os.environ.get('LLM_API_KEY') or None,
            enabled=True,
            auto_analyze=llm_auto_analyze,
            timeout=int(os.environ.get('LLM_TIMEOUT', 60))
        )
        logging.info(f'LLM analyzer initialized: model={llm_analyzer.model_name}, auto={llm_auto_analyze}')
    else:
        logging.info('LLM analyzer disabled')

    ha = HAIntegration(state_file=state_file, update_interval=1.0)
    recorder = RecordingManager(
        stream_url=stream_url,
        recordings_path=recordings_path,
        pre_roll=pre_roll,
        post_roll=post_roll,
        max_recordings=max_recordings,
        llm_analyzer=llm_analyzer,
        llm_auto_analyze=llm_auto_analyze,
        hls_follow=os.environ.get('RECORDING_MODE', 'ffmpeg').strip().lower() == 'hls_follow',
    )

    def on_motion_start(event: MotionEvent):
        logging.info(f'Event started ({event.trigger}), area: {event.motion_area}, person: {event.person_score}')
        recorder.start_recording(
            motion_start_time=event.timestamp,
            trigger=event.trigger,
            person_score=event.person_score,
            snapshot=event.snapshot,
        )
        ha.update_recording_state(
            is_recording=True,
            total_recordings=len(recorder.get_recordings())
        )

    def on_motion_end(event: MotionEvent):
        logging.info('Motion ended, scheduling recording stop')
        recorder.schedule_stop()

    def on_motion_frame(event: MotionEvent):
        # Extend recording while motion continues
        recorder.extend_recording()

    def on_person(event: MotionEvent):
        # Person seen during an event: upgrade it and keep the best snapshot
        recorder.update_event(trigger='person', person_score=event.person_score, snapshot=event.snapshot)

    detector = MotionDetector(
        stream_url=stream_url,
        motion_threshold=motion_threshold,
        min_duration=min_duration,
        check_interval=1.0,
        motion_cooldown=motion_cooldown,
        roi_x_start=roi_x_start,
        roi_x_end=roi_x_end,
        roi_y_start=roi_y_start,
        roi_y_end=roi_y_end,
        settings_file=settings_file,
        on_motion_start=on_motion_start,
        on_motion_end=on_motion_end,
        on_motion_frame=on_motion_frame,
        on_person=on_person,
        fps=detect_fps,
        person_model=person_model,
        person_score=person_score,
        person_input_size=person_input_size,
        motion_fallback=motion_fallback,
    )

    running = True

    def stop(signum, _frame):
        nonlocal running
        logging.info(f'Signal {signum} received, shutting down...')
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    ha.start()
    # Buffer disabled: HLS playlist window provides natural pre-roll (~6-10s).
    # Running a second ffmpeg for buffering causes stream contention (stutters/repeated frames).
    detector.start()
    logging.info('Motion detection service started')

    while running:
        stats = detector.get_stats()
        ha.update_motion_state(
            detected=detector.is_motion_active,
            state=stats['state'],
            frames_processed=stats['frames_processed']
        )
        ha.update_detector_stats({
            'person_detected': stats['person_detected'],
            'event_trigger': stats['event_trigger'],
            'last_person_score': stats['last_person_score'],
            'person_events': stats['person_events'],
            'scene': stats['scene'],
            'brightness': stats['brightness'],
            'light_changes': stats['light_changes'],
            'camera_moves': stats['camera_moves'],
            'person_detector': stats['person_detector'],
            'person_ms': stats['person_ms'],
            'stream_restarts': stats['stream_restarts'],
        })

        rec_stats = recorder.get_stats()
        latest = recorder.get_latest_recording()
        ha.update_recording_state(
            is_recording=rec_stats['is_recording'],
            total_recordings=rec_stats['total_recordings'],
            latest_recording=latest.filename if latest else None,
            latest_recording_time=latest.start_time if latest else None,
            latest_thumbnail=latest.thumbnail if latest else None
        )
        time.sleep(1)

    detector.stop()
    recorder.stop_recording_immediate()
    ha.stop()


if __name__ == '__main__':
    main()
