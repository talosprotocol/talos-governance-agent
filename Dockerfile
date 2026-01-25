FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgmp-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --upgrade pip setuptools wheel
RUN pip install .

ENV PYTHONPATH=/app/src
CMD ["python", "src/talos_governance_agent/main.py"]
