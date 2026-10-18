FROM python:3.12-slim
# APP_ORIGIN matches PORT for a local `docker run -p 8080:8080`; Cloud Run sets its own HTTPS origin.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 APP_ORIGIN=http://localhost:8080
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 echocheck \
    && useradd --uid 10001 --gid 10001 --no-create-home echocheck \
    && mkdir -p /var/lib/echocheck/audio \
    && chown -R 10001:10001 /var/lib/echocheck
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY frontend ./frontend
# The practice catalog is read only in prototype mode (ECHOCHECK_MODE=prototype).
COPY dataset/demo_scenarios_v1.json ./dataset/demo_scenarios_v1.json
ENV ECHOCHECK_DB=/var/lib/echocheck/echocheck.db ECHOCHECK_AUDIO_DIR=/var/lib/echocheck/audio
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "backend.run"]
