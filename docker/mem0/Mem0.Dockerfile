FROM python:3.12.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir \
    mem0ai==2.0.11 \
    fastapi==0.136.3 \
    uvicorn==0.49.0 \
    qdrant-client==1.15.1

WORKDIR /app
COPY mem0_sidecar.py /app/mem0_sidecar.py
COPY mem0_env.py /app/mem0_env.py

EXPOSE 8000
CMD ["uvicorn", "mem0_sidecar:app", "--host", "0.0.0.0", "--port", "8000"]
