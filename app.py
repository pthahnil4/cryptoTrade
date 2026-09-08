"""
CryptoTrade 启动入口
====================
项目的根级启动脚本，导入 crypto 模块中的 Flask 应用并运行。
"""
import sys
import os
import warnings

# Python 3.13 已弃用 datetime.datetime.utcnow()，okx SDK 内部仍在使用（第三方库代码无法修改），
# 每次发请求都会在终端刷屏两遍 DeprecationWarning。这里精确屏蔽该弃用警告，不影响其他警告。
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r"datetime\.datetime\.utcnow\(\) is deprecated",
)

# Windows 无控制台/GBK 环境下 print 含 emoji 会抛 UnicodeEncodeError 导致进程直接退出，
# 这里统一将标准输出重配置为 UTF-8（无法替换的字符降级处理）
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 确保项目根目录在 sys.path 中，以便导入 crypto 包
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crypto.app import app

if __name__ == '__main__':
    print("[启动] 前后端分离服务已启动！")
    print("[启动] 请在浏览器访问: http://127.0.0.1:5000")
    print("[启动] 加密货币监控台: http://127.0.0.1:5000/")
    print("[启动] API控制台: http://127.0.0.1:5000/api-console")
    # threaded=True：Flask 开发服务器默认单线程，并发请求（页面加载+多个fetch）
    # 会互相阻塞甚至导致进程异常退出，开启多线程保证接口可用性
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
