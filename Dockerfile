FROM python:3.11-slim

# Install ffmpeg and build tools
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV OUTPUT_DIR=/app/reels_output
RUN mkdir -p /app/reels_output

EXPOSE 8080

CMD ["python3", "app.py"]
