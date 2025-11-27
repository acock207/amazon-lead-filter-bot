# Dockerfile — simple, uses OCR.space (no local tesseract binary)
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt requirements.txt
RUN pip install -U pip && pip install -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["python", "main.py"]
