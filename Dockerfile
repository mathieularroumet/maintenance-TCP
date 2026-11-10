FROM python:3.12-slim
WORKDIR /app
COPY server.py .
ENV DATA_DIR=/data PORT=8000 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1
CMD ["python", "server.py"]
