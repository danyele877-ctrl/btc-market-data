FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app
COPY candle_service.py live_radar.py build_radar_snapshot.py capture_render_feed.py render_bridge_publication.py read_current_alert.py telegram_alert.py zec_*.py ./
RUN mkdir -p /app/data && chown -R app:app /app
USER app

EXPOSE 8080
CMD ["python", "candle_service.py"]
