FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV HOST=0.0.0.0 PORT=8765 DATA_DIR=/data TZ=Europe/Paris PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "app.py"]
