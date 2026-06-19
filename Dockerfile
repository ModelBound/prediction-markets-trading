FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py .

# Create data directory
RUN mkdir -p data

# Run the trading agent
CMD ["python", "main.py"]
