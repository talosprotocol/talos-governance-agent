FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install .

COPY src/ ./src/
RUN pip install -e .

ENV PYTHONPATH=/app/src
CMD ["python", "src/talos_governance_agent/main.py"]
