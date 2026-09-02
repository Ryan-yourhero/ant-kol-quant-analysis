"""快速测试：验证 deepseek-v4-pro 对 json_object 和不同 max_tokens 的支持"""
import sys, logging, time
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser.llm_client import chat_completion

# 测试1：小JSON，json_object格式
print("=== 测试1: 小JSON + json_object ===")
t0 = time.time()
r = chat_completion(
    [{"role": "user", "content": "返回JSON: {\"hello\": \"world\"}"}],
    max_tokens=200,
    timeout=120,
    response_format={"type": "json_object"},
)
print(f"  耗时: {time.time()-t0:.1f}s, 结果: {r.get('ok')}, 长度: {len(r.get('content',''))}")

# 测试2：小JSON，无json_object
print("=== 测试2: 小JSON 无格式约束 ===")
t0 = time.time()
r = chat_completion(
    [{"role": "user", "content": "返回JSON: {\"hello\": \"world\"}"}],
    max_tokens=200,
    timeout=120,
)
print(f"  耗时: {time.time()-t0:.1f}s, 结果: {r.get('ok')}, 长度: {len(r.get('content',''))}")

# 测试3：大max_tokens，无json_object
print("=== 测试3: max_tokens=8000 无json_object ===")
t0 = time.time()
r = chat_completion(
    [{"role": "user", "content": "回复一个简短的JSON数组(约50条): [{\"a\":1},{\"a\":2},...]"},],
    max_tokens=8000,
    timeout=300,
)
print(f"  耗时: {time.time()-t0:.1f}s, 结果: {r.get('ok')}, 长度: {len(r.get('content',''))}")
