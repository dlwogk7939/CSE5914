FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt \
    && useradd --uid 10001 --create-home app
COPY backend/main.py ./backend/main.py
COPY backend/espn.py ./backend/espn.py
COPY --chown=app:app nba.db ./nba.db
RUN chown app:app /app
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
