@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [home] готовлю venv...
  python -m venv .venv || goto :error
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\python -m pip install --quiet -r requirements.txt || goto :error
)
.venv\Scripts\python app.py %*
exit /b %errorlevel%
:error
echo [home] не удалось подготовить окружение
exit /b 1
