@echo off
cd /d "%~dp0"
echo ========================================
echo   Outbound OS - Iniciando...
echo ========================================
echo.

docker info >nul 2>&1
if errorlevel 1 (
    echo ERRO: Docker Desktop nao esta rodando.
    echo Abra o Docker Desktop e tente novamente.
    echo.
    pause
    exit /b 1
)

echo Servidor subindo em http://localhost:7860 ...
echo.

rem Aguarda o servidor e abre em modo app
start "" /b cmd /c ^
  "for /l %%i in (1,1,20) do (timeout /t 2 >nul & curl -s http://localhost:7860 >nul 2>&1 && (start chrome --app=http://localhost:7860 --window-size=1360,860 || start msedge --app=http://localhost:7860 || start http://localhost:7860) && exit)"

docker compose up --build
