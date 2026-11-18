FROM python:3.12-slim
RUN pip install --no-cache-dir "flask>=3,<4"
WORKDIR /app
COPY app.py .
ENV DB_PATH=/data/ledger.db APP_TZ=America/Los_Angeles PYTHONUNBUFFERED=1
EXPOSE 8088
CMD ["python", "app.py"]
