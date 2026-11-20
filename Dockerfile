# ai-tracker server. Build from the repo root: it imports the tracker's time model.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt
COPY tracker tracker
COPY server server
RUN useradd --uid 10001 app && chown -R app /app
USER app
EXPOSE 8000
# No --proxy-headers: the app never reads the client address or scheme (links use PUBLIC_ORIGIN), so there is
# no reason to trust X-Forwarded-* headers that anyone reaching the container directly could set.
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
