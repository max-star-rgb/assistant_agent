FROM ghcr.io/vectorize-io/hindsight:0.8.4@sha256:2c60f233eaba8f51db31adb920a560735aaf6f314e4b63c36c73159742dfa1a7

# Preserve Hindsight's default local reranker while making first startup
# independent of a runtime Hugging Face download.
RUN /app/api/.venv/bin/python -c 'from sentence_transformers import CrossEncoder; CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", device="cpu", model_kwargs={"low_cpu_mem_usage": False})'

ENV HF_HUB_OFFLINE=1
