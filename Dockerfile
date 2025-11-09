# Dockerfile — simple, uses OCR.space (no local tesseract binary)
FROM python:3.11-slim
WORKDIR /app
COPY deploy/requirements.txt requirements.txt
RUN pip install -U pip && pip install -r requirements.txt
COPY . .
# Expose port 8080 for health checks (Digital Ocean/cloud platforms)
EXPOSE 8080
CMD ["python", "main.py"]
