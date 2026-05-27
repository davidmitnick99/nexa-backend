# Use a lightweight, official Python runtime as a parent image
FROM python:3.11-slim

# Set system environment variables to optimize Python inside Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies needed for network handshakes
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy over the dependency definitions first to leverage Docker caching
COPY requirements.txt .

# Install your Python package stacks
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your local application code into the container
COPY . .

# Expose port 8080 (Google Cloud Run expects containers to listen on 8080)
EXPOSE 8080

# Command to launch Uvicorn bound to Cloud Run's port requirement
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
