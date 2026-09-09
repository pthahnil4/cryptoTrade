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
from crypto.web_auth import configured_token as _web_token

if __name__ == '__main__':
    # 监听地址/端口可用环境变量覆盖（默认仍 0.0.0.0 + 5000，兼容现有部署）。
    # 安全不靠改这里：真正兜底的是 crypto/web_auth.py 的访问闸门——
    # 没配口令时远程一律 403（等价于只绑本机），配了口令时远程输口令进入，
    # 因此绑 0.0.0.0 也不再是“实盘接口裸奔”。
    _host = (os.environ.get('CRYPTO_WEB_HOST') or '0.0.0.0').strip()
    _port = int(os.environ.get('CRYPTO_WEB_PORT') or 5000)

    print("[启动] 前后端分离服务已启动！")
    print(f"[启动] 请在浏览器访问: http://127.0.0.1:{_port}")
    print(f"[启动] 加密货币监控台: http://127.0.0.1:{_port}/")
    print(f"[启动] API控制台: http://127.0.0.1:{_port}/api-console")
    if _web_token():
        print("[安全] 访问口令已启用（CRYPTO_WEB_TOKEN 或 data/web_token.txt）："
              "所有设备都要先在登录页输一次口令")
    else:
        print(f"[安全] 未配置访问口令 → 只允许本机访问，从其它设备打开 "
              f"http://{_host}:{_port} 会被 403 拦下")
        print("[安全] 想让手机/电脑远程用：设 CRYPTO_WEB_TOKEN 环境变量，"
              "或把口令写进 data/web_token.txt（已在 .gitignore），重启生效")
    # threaded=True：Flask 开发服务器默认单线程，并发请求（页面加载+多个fetch）
    # 会互相阻塞甚至导致进程异常退出，开启多线程保证接口可用性
    app.run(debug=False, host=_host, port=_port, threaded=True)
