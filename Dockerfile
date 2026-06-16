FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MULTIMODAL_AGENT_VISION_PROVIDER=mock
ENV MULTIMODAL_AGENT_CHAT_PROVIDER=mock
ENV MULTIMODAL_AGENT_IMAGE_PROVIDER=mock
ENV MULTIMODAL_AGENT_PRODUCT_PROVIDER=mock
ENV MULTIMODAL_AGENT_PRICE_PROVIDER=mock
ENV MULTIMODAL_AGENT_RENDER_PROVIDER=mock
ENV MULTIMODAL_AGENT_VIDEO_PROVIDER=mock
ENV MULTIMODAL_AGENT_INTENT_ROUTER=rule
ENV RUN_INTEGRATION_TESTS=0

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY demo_data ./demo_data

RUN pip install --no-cache-dir .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)); raise SystemExit(0 if data.get('status') == 'ok' else 1)"

CMD ["uvicorn", "multimodal_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
