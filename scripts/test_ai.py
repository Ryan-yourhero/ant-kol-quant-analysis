"""Quick test: AI connection + full parse with AI"""
import sys
sys.path.insert(0, ".")

from src.parser.llm_client import is_configured, chat_completion, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

print("AI 配置:")
print(f"  API Key: {LLM_API_KEY[:12]}...{LLM_API_KEY[-4:]}")
print(f"  Base URL: {LLM_BASE_URL}")
print(f"  Model: {LLM_MODEL}")
print(f"  Configured: {is_configured()}")
print()

# Quick connection test
print("测试 AI 连接...")
result = chat_completion([
    {"role": "user", "content": "回复：OK"}
], max_tokens=10, timeout=30)

if result["ok"]:
    print(f"  AI 连接成功: {result['content'][:100]}")
else:
    print(f"  AI 连接失败: {result['error']}")
    sys.exit(1)

print()

# Full parse test with AI
print("=" * 60)
print("开始完整解析测试（AI 模式）...")
from src.parser import parse_md_to_excel

md_path = "output/screen_dump_20260805_162701.md"
excel_path, result = parse_md_to_excel(md_path, use_ai=True)

print(f"\n结果:")
print(f"  Excel: {excel_path}")
print(f"  记录数: {result.total_records}")
print(f"  AI 辅助: {'是' if result.ai_used else '否'}")
