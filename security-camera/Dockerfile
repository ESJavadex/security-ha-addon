ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.19

ARG BUILD_VERSION=dev
ENV ADDON_VERSION=${BUILD_VERSION}

# Install system dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip \
    ffmpeg \
    dos2unix \
    jq \
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
    watchdog \
    Pillow

# Create directories
RUN mkdir -p /app /share/security_recordings

# Cache buster - change this value to force rebuild of COPY layers
ARG CACHE_BUST=20260712-recording-v3

# Copy application files
COPY motion_detector.py /app/
COPY recording_manager.py /app/
COPY ha_integration.py /app/
COPY http_server.py /app/
COPY llm_analyzer.py /app/
COPY run.sh /app/

# Fix line endings and permissions
RUN dos2unix /app/run.sh && chmod a+x /app/run.sh

WORKDIR /app

CMD ["/app/run.sh"]
