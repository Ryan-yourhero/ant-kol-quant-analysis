import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s:%(name)s:%(message)s")

from src.parser.llm_client import chat_completion, LLM_MODEL, LLM_BASE_URL

print(f"Model: {LLM_MODEL}")
print(f"URL: {LLM_BASE_URL}")

result = chat_completion(
    [{"role": "user", "content": "你好，请回复 1"}],
    max_tokens=100,
    timeout=60,
)
print(f"Result: {result}")
