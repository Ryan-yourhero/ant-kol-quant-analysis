"""最小复现：打开traceback看' int object is not iterable'到底哪一行"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import traceback
from core.data_extractor import OperationDataExtractor

ext = OperationDataExtractor()

texts = [
    "童童读财",
    "14:39",
    "朱雀企业优胜股票C",
    "买入确认中",
    "买入金额(元)",
    "2,000.00元",
]

try:
    ops = ext.extract_from_flat_texts(texts)
    print("ops count:", len(ops))
    for op in ops:
        print(op.to_dict())
except Exception as e:
    print("TOP LEVEL EXCEPTION:", repr(e))
    traceback.print_exc()
