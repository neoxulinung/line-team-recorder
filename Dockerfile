FROM python:3.12-slim

# Confirmed live during Phase 1 smoke testing: stdout is block-buffered when not attached to a
# TTY (i.e. always, in a container), so print()-based error logs (see log lines in main.py,
# fact_check.py) can sit unflushed indefinitely - Cloud Logging would show nothing until the
# buffer happens to fill. Unbuffered stdout is what makes those logs actually show up.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ .

# Cloud Run injects $PORT at runtime - shell form so it actually expands.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
