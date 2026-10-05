#!/bin/sh
# One-shot verification pipeline used by the compose "verify" service.
#   1. run the code tests
#   2. build the application (byte-compile)
#   3. submit the business smoke request (contains both an exact threshold
#      intersection and a coverage gap) against the running API
# Reports the overall result purely via its exit code.
set -eu

cd "$(dirname "$0")"

echo "==> [1/3] 运行代码测试"
python3 -m unittest discover -s tests -v

echo "==> [2/3] 构建应用（字节码编译）"
python3 -m compileall -q app

echo "==> [3/3] 提交业务冒烟请求（阈值交点 + 覆盖缺口）"
API_BASE="${API_BASE:-http://api:8000}" python3 smoke.py

echo "==> VERIFY OK"
