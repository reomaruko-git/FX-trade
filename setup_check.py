"""
インストール確認スクリプト
実行方法: python setup_check.py
"""

def check_libraries():
    results = []

    # oandapyV20
    try:
        import oandapyV20
        results.append(f"✅ oandapyV20: OK (バージョン {oandapyV20.__version__})")
    except ImportError:
        results.append("❌ oandapyV20: インストールされていません")

    # pandas
    try:
        import pandas as pd
        results.append(f"✅ pandas    : OK (バージョン {pd.__version__})")
    except ImportError:
        results.append("❌ pandas: インストールされていません")

    # numpy
    try:
        import numpy as np
        results.append(f"✅ numpy     : OK (バージョン {np.__version__})")
    except ImportError:
        results.append("❌ numpy: インストールされていません")

    print("=" * 45)
    print("  ライブラリ インストール確認")
    print("=" * 45)
    for r in results:
        print(r)
    print("=" * 45)

    if all("✅" in r for r in results):
        print("🎉 すべて準備OKです！")
    else:
        print("⚠️  上記の❌をインストールしてください。")
        print("   pip install -r requirements.txt")

if __name__ == "__main__":
    check_libraries()
