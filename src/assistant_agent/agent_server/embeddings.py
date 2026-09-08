"""Agent Server entry point for the native Store's text index."""

from langchain_openai import OpenAIEmbeddings

from assistant_agent.config import load_app_config


async def embed_memory_texts(texts: list[str]) -> list[list[float]]:
    config = load_app_config()
    if config.provider_mode != "real":
        raise ValueError("Memory semantic indexing requires explicit real provider mode")
    memory = config.memory
    if not memory.embedding_api_key:
        raise ValueError("Memory semantic indexing requires QWEN_EMBEDDING_API_KEY or QWEN_API_KEY")
    embeddings = OpenAIEmbeddings(
        model="text-embedding-v4",
        dimensions=1024,
        api_key=memory.embedding_api_key,
        base_url=memory.embedding_base_url,
        check_embedding_ctx_length=False,
        chunk_size=10,
        model_kwargs={"encoding_format": "float"},
    )
    return await embeddings.aembed_documents(texts)
