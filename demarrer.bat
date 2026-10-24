@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Smartovate - Systeme Multi-Agents ===

rem Le projet a besoin de Python 3.10 a 3.13 (les versions plus recentes n'ont pas encore
rem tous les composants precompiles pour Windows).
set "CHECK=import sys;sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,13) else 1)"
set "PY="
for %%V in (3.12 3.13 3.11) do (
  if not defined PY py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
)

if not defined PY (
  where py >nul 2>nul && (
    echo Python 3.12 n'est pas installe : installation automatique via le gestionnaire Python...
    py install 3.12
    py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
  )
)
if not defined PY (
  python -c "%CHECK%" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo Python 3.12 est necessaire et n'a pas ete trouve.
  echo 1. Ouvrez https://www.python.org/downloads/windows/
  echo 2. Telechargez un "Windows installer (64-bit)" de Python 3.12.
  echo 3. Cochez "Add python.exe to PATH", puis Install Now.
  echo 4. Redemarrez l'ordinateur et relancez ce fichier.
  start https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
echo Python utilise :
%PY% --version

rem Supprimer un ancien environnement cree avec une version incompatible
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "%CHECK%" >nul 2>nul || (
    echo Suppression d'un ancien environnement incompatible...
    rmdir /s /q .venv
  )
)

if not exist .venv\Scripts\python.exe (
  echo [1/3] Creation de l'environnement Python...
  %PY% -m venv .venv
)
echo [2/3] Installation des dependances (5 a 10 minutes la premiere fois)...
call .venv\Scripts\python.exe -m pip install -q --upgrade pip
call .venv\Scripts\python.exe -m pip install -q --only-binary=:all: -r requirements.txt
if errorlevel 1 ( echo Echec de l'installation : envoyez une capture de ce message. & pause & exit /b 1 )

if not exist .env copy .env.example .env >nul

echo [3/3] Demarrage de l'API et de l'interface...
start "API Multi-Agents (ne pas fermer)" cmd /k ".venv\Scripts\python.exe -m uvicorn api.main:app --port 8000"
timeout /t 5 /nobreak >nul
start "Interface Multi-Agents (ne pas fermer)" cmd /k ".venv\Scripts\python.exe -m streamlit run ui\streamlit_app.py --server.port 8501"
timeout /t 8 /nobreak >nul
start http://localhost:8501
echo.
echo L'interface s'ouvre dans votre navigateur : http://localhost:8501
echo Pour arreter : fermez les deux fenetres "API" et "Interface".
pause
