import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s:%(name)s:%(message)s")

from src.parser.llm_client import chat_completion

# 只取 MD 的前 2000 字符（页面1+页面2的前面部分）
with open("output/screen_dump_20260811_144955.md", encoding="utf-8") as f:
    text = f.read()

# 只发页面1+2
text = text[:3000]

result = chat_completion(
    [
        {"role": "system", "content": "返回JSON: {\"test\": true}"},
        {"role": "user", "content": f"返回 {{\"len\": {len(text)}}}"},
    ],
    max_tokens=100,
    timeout=60,
)
print(f"OK: {result['ok']}, len(content)={len(result.get('content',''))}")
print(f"Content[:200]: {result.get('content','')[:200]}")
