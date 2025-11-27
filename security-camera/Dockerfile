ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install system dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip \
    ffmpeg \
    dos2unix \
    # OpenCV dependencies
    py3-numpy \
    opencv \
    py3-opencv

# Create virtual environment
ENV VIRTUAL_ENV=/app/venv
RUN python3 -m venv $VIRTUAL_ENV --system-site-packages
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Install Python packages
RUN pip install --no-cache-dir \
    requests \
    watchdog

# Create directories
RUN mkdir -p /app /share/security_recordings

# Copy application files
COPY motion_detector.py /app/
COPY recording_manager.py /app/
COPY ha_integration.py /app/
COPY http_server.py /app/
COPY run.sh /app/

# Fix line endings and permissions
RUN dos2unix /app/run.sh && chmod a+x /app/run.sh

WORKDIR /app

CMD ["/app/run.sh"]
