#!/usr/bin/env python3
"""独立 Flask 启动脚本 — 避免 shell 内联 Python 的引号/特殊字符解析问题"""
import sys
import os

sys.path.insert(0, '/app')
os.chdir('/app')

from web.app import app

app.run(
    host='0.0.0.0',
    port=int(os.getenv('FLASK_PORT', '5000')),
    threaded=True
)
