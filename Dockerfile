# Jinora Enterprise Container
FROM python:3.11-slim

# Install system dependencies including libmediainfo for pymediainfo
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmediainfo0v5 \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

EXPOSE 8000

CMD ["python", "start_server.py"]
