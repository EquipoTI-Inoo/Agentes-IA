# Jinora Enterprise Container
FROM python:3.11-slim

# Install system dependencies including FFmpeg and curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" jinja2 aiofiles sqlalchemy psycopg[binary]

# Copy project files
COPY . .

EXPOSE 8000

CMD ["python", "start_server.py"]
