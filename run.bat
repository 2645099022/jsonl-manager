@echo off
REM Claude Code JSONL 会话管理 — Windows 启动脚本
cd /d "%~dp0"
python app.py %*
