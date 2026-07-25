FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The resolver reuses the discovery introspection + LLM modules. Build context is the
# repo root so we can pull them from ../discovery (see cloudbuild invocation / deploy).
COPY main.py .
COPY discovery_lib.py .
COPY llm.py .

CMD ["python", "main.py"]
