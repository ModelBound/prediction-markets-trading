FROM python:3.12-slim

WORKDIR /app

# Node.js + mcporter for optional Exa semantic search (Agent-Reach backend).
# DuckDuckGo + Jina Reader work without Node; Exa is preferred when available.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && npm install -g mcporter \
    && apt-get purge -y npm \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

RUN mkdir -p data

CMD ["python", "main.py"]
