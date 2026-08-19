# NOTE: pendulum 2.x (used by the forked scheduler code) cannot build on
# Python 3.12+ because distutils was removed. python:3.11-slim still ships
# distutils, so pendulum 2.x builds here. Follow-up: migrate to pendulum 3.x.
# build-essential + python3-dev are needed to compile pendulum 2.x from source
# (no prebuilt wheel for this Python version).
FROM python:3.11-slim

# ffmpeg is required for the actual stream recording
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY recording-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY recording-service/ /app/

EXPOSE 8000

CMD ["python", "main.py"]
