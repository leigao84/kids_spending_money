FROM python:3.12-slim
RUN pip install --no-cache-dir psycopg2-binary
COPY sync_pocket_money.py /sync_pocket_money.py
CMD ["python3", "/sync_pocket_money.py"]
