# build context is the project root: the demo reuses api/app (reserve()) and scripts/ (race, replay, chaos)
FROM python:3.12-slim
WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/app api/app
COPY scripts scripts
COPY demo demo
EXPOSE 8080
CMD ["uvicorn", "main:app", "--app-dir", "demo", "--host", "0.0.0.0", "--port", "8080"]
