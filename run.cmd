@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
if not defined MQQUANT_SOURCE_ROOT set "MQQUANT_SOURCE_ROOT=%SCRIPT_DIR%bundle"
py -m streamlit run app.py
