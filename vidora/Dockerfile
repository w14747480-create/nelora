FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt
COPY . .
CMD gunicorn -b 0.0.0.0:${PORT:-10000} -w 2 --threads 4 -t 180 app:app
