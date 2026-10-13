# Deno is the JavaScript runtime yt-dlp needs for YouTube.
ARG DENO_TAG=bin
FROM denoland/deno:${DENO_TAG} AS deno

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

# German voice for text-to-speech (Piper "Thorsten", high quality).
# Create the folder first: ADD --chmod would also apply 644 to a folder it creates, which
# makes it unreadable for the app user.
RUN mkdir -p -m 755 /opt/piper
ARG PIPER_VOICE=https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx
ADD --chmod=644 ${PIPER_VOICE} /opt/piper/voice.onnx
ADD --chmod=644 ${PIPER_VOICE}.json /opt/piper/voice.onnx.json

# The virtualenv belongs to the app user so yt-dlp can update itself at startup.
RUN useradd --system --uid 1000 --create-home --home-dir /home/soundboard soundboard \
    && python -m venv /opt/venv \
    && chown -R soundboard /opt/venv \
    && mkdir -p /sounds /data /collections \
    && chown soundboard /sounds /data
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY requirements.txt .
USER soundboard
RUN pip install -r requirements.txt \
    && python -c "open('/opt/piper/voice.onnx', 'rb').read(1); open('/opt/piper/voice.onnx.json').read(1)"
# Discord in its own step: if the voice packages (PyNaCl, davey for Discord's end-to-end encryption)
# ever fail to install, the Mumble bot still builds and the Discord status page says what is missing.
COPY requirements-discord.txt .
RUN pip install -r requirements-discord.txt \
    || pip install "discord.py>=2.7.1,<2.8" \
    || echo "WARNUNG: discord.py konnte nicht installiert werden - nur Mumble verfuegbar"

COPY app ./app
COPY static ./static
COPY tests ./tests

ENV SOUNDS_DIR=/sounds \
    DATA_DIR=/data \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080
EXPOSE 8080
VOLUME ["/sounds", "/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

# Which commit this image was built from, shown in the web interface and in /help.
# Last on purpose: a new commit only rebuilds this metadata layer.
ARG BARD_BOARD_REVISION=""
ARG BARD_BOARD_DATE=""
ENV BARD_BOARD_REVISION=$BARD_BOARD_REVISION \
    BARD_BOARD_DATE=$BARD_BOARD_DATE

CMD ["python", "-m", "app.main"]
