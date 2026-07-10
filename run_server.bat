@echo off
REM ============================================================
REM  RSG CFS15 MCP Server launcher
REM  Requires: Windows, Python 3.10+, RSG CFS 15 (licensed w/ Automation)
REM  Deps: pip install -r requirements.txt   (mcp, pywin32)
REM ============================================================
cd /d "%~dp0"
set PYTHONPATH=%~dp0
python -m cfs_rsg_mcp
