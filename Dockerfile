FROM python:3.14-slim

# ffmpeg is required for the actual stream recording
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY recording-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY recording-service/ /app/

EXPOSE 8000

CMD ["python", "main.py"]
