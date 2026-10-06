"""图形界面入口：``python run_gui.py``。

给非技术用户的最短路径：
1. "嵌入指纹" 页：选图片目录 → 填买家标识 → 点开始；
2. 把输出目录里的图发给买家；
3. "溯源提取" 页：把可疑图片拖进来 → 点开始 → 直接看到买家标识。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from penguinprint.gui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
