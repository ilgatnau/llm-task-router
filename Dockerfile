FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY router ./router
COPY rules.yaml free-pool.yaml online-pool.yaml ./
CMD ["uvicorn", "router.app:app", "--host", "0.0.0.0", "--port", "8000"]
