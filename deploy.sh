#!/bin/bash
# workbuddy-desktop-api 一键部署脚本
# 用法: 进入目录，运行 ./deploy.sh

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}=== WorkBuddy Desktop-api 部署 ===${NC}"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗ 未找到 Python3${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Python: $(python3 --version | awk '{print $2}')${NC}"

# 虚拟环境
if [ ! -d "venv" ]; then
    echo "创建虚拟环境..."
    python3 -m venv venv
fi

# 安装依赖
echo "安装依赖..."
source venv/bin/activate
pip install --upgrade pip -q 2>/dev/null
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple -q

# 配置文件
if [ ! -f config.json ]; then
    cp config.example.json config.json
    echo "已创建 config.json（默认 admin/admin，sk-workbuddy）"
fi

echo ""
echo -e "${GREEN}✓ 部署完成！${NC}"
echo ""
echo "启动: ./venv/bin/python main.py"
echo "后台: nohup ./venv/bin/python main.py > desktop.log 2>&1 &"
echo "停止: pkill -f 'python main.py'"
echo "面板: http://127.0.0.1:8080  （admin / admin）"
echo "局域网: http://<本机IP>:8080   （默认监听 0.0.0.0）"
echo "导入前请先退出 WorkBuddy Desktop（释放 cookie 库锁）"
echo ""
