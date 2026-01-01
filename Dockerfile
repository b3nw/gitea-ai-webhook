
FROM python:3.11-slim

# Install system dependencies
# git is required for cloning repos
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the AI Review tool
# Installing directly from PyPI
RUN pip install --no-cache-dir xai-review

# Copy application code
COPY webhook_server.py .

# Expose port
EXPOSE 3000

# Command to run the server
CMD ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "3000"]
