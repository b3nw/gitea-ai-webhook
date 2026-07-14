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

# Install the AI Review tool from a pinned GitHub commit (not PyPI)
RUN pip install --no-cache-dir \
  "git+https://github.com/b3nw/ai-review.git@764b124f155707f36ea38d327f0c443b65131ad4"

# Copy custom prompt templates
COPY prompts/ /app/prompts/

# Copy application code
COPY webhook_server.py .

# Expose port
EXPOSE 3000

# Command to run the server
CMD ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "3000"]
