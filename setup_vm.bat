@echo off
title Installation Automatique Agent VM
echo ===================================================
echo   PREPARATION DE LA VM (PYTHON + C++ + PARE-FEU)
echo ===================================================

:: 1. Ouverture du port 65432 dans le pare-feu Windows
netsh advfirewall firewall add rule name="AgentServerVM" dir=in action=allow protocol=TCP localport=65432 >nul 2>&1

:: 2. Verification et installation de Microsoft Visual C++ Redistributable
if not exist "%SystemRoot%\System32\vcruntime140_1.dll" (
    echo [1/3] Installation de Microsoft Visual C++ Redistributable...
    winget install --id Microsoft.VCRedist.2015+.x64 -e --source winget --silent --accept-source-agreements --accept-package-agreements
) else (
    echo [1/3] Microsoft Visual C++ Redistributable deja present.
)

:: 3. Verification et installation de Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [2/3] Python absent. Installation de Python 3.12 via Winget...
    winget install --id Python.Python.3.12 -e --source winget --silent --accept-source-agreements --accept-package-agreements
    set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%ProgramFiles%\Python312;%ProgramFiles%\Python312\Scripts;%PATH%"
) else (
    echo [2/3] Python deja installe.
)

:: 4. Lancement du serveur qui installera automatiquement les paquets pip et Brave
echo [3/3] Demarrage de agent_server.py...
cd /d "%~dp0"
python agent_server.py
pause
