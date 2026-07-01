#!/usr/bin/env bash
# Claude Code JSONL 会话管理 — Linux/Mac 启动脚本
set -e
cd "$(dirname "$0")"
python3 app.py "$@"
