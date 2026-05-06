@echo off
REM ============================================================================
REM Veille Tech - script de lancement pour Windows
REM Double-clique sur ce fichier ou lance-le depuis l'Explorateur.
REM
REM Etapes :
REM   1. Verifie que Python est installe (sinon ouvre la page de telechargement)
REM   2. Cree l'environnement virtuel .venv si absent
REM   3. Installe / met a jour les dependances depuis requirements.txt
REM   4. Lance configurer.py si .env n'existe pas (assistant de config)
REM   5. Propose un menu modifier/lancer si .env existe et est complet
REM   6. Demarre main.py (pipeline complet)
REM ============================================================================
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Veille Tech - Lancement

echo.
echo ===============================================================
echo   VEILLE TECHNOLOGIQUE - Lanceur Windows
echo ===============================================================
echo.

REM ===== Etape 1 : Python installe ? =====
python --version >nul 2>&1
if errorlevel 1 goto no_python

for /f "tokens=*" %%v in ('python --version') do echo [OK] %%v detecte

REM ===== Etape 2 : Environnement virtuel =====
if not exist ".venv\Scripts\python.exe" goto create_venv
goto activate_venv

:create_venv
echo.
echo [.] Creation de l'environnement virtuel .venv ...
python -m venv .venv
if errorlevel 1 goto venv_failed
echo [OK] .venv cree.

:activate_venv
call ".venv\Scripts\activate.bat"

REM ===== Etape 3 : Dependances =====
echo.
echo [.] Verification des dependances ...
python -m pip install --quiet --upgrade pip >nul 2>&1
python -m pip install --quiet -r requirements.txt
if errorlevel 1 goto deps_failed
echo [OK] Dependances OK.

REM ===== Etape 4 : .env present et valide ? =====
if not exist ".env" goto first_run
python configurer.py --check
if errorlevel 1 goto incomplete_config
goto modify_or_run

REM ----------------------------------------------------------------------------
:first_run
echo.
echo ===============================================================
echo   PREMIERE UTILISATION DETECTEE
echo ===============================================================
echo.
echo Le fichier .env n'existe pas encore. Tu vas le creer
echo via un assistant interactif qui t'expliquera ou obtenir
echo chaque cle API necessaire.
echo.
pause
python configurer.py
if errorlevel 1 goto config_failed
goto run_pipeline

REM ----------------------------------------------------------------------------
:incomplete_config
echo.
echo [!] La configuration .env est incomplete.
echo Lancement automatique de l'assistant de configuration...
echo.
python configurer.py
if errorlevel 1 goto config_failed
goto run_pipeline

REM ----------------------------------------------------------------------------
:modify_or_run
echo.
echo ===============================================================
echo   Veux-tu modifier ta configuration avant de lancer ?
echo ===============================================================
echo.
echo   [C] Modifier mes cles API et emails (assistant pas-a-pas)
echo   [L] Lancer le pipeline directement avec la config actuelle
echo.
choice /c CL /n /m "Ton choix (C ou L) : "
if errorlevel 2 goto run_pipeline
python configurer.py
goto run_pipeline

REM ----------------------------------------------------------------------------
:run_pipeline
echo.
echo ===============================================================
echo   DEMARRAGE DU PIPELINE
echo ===============================================================
echo.
python main.py
goto end

REM ----------------------------------------------------------------------------
:no_python
echo.
echo [X] Python n'est pas installe sur ce systeme.
echo.
echo Veuillez installer Python 3.12 ou plus recent depuis :
echo   https://www.python.org/downloads/windows/
echo.
echo IMPORTANT : pendant l'installation, COCHER la case
echo "Add python.exe to PATH" sur le premier ecran.
echo.
start https://www.python.org/downloads/windows/
pause
exit /b 1

:venv_failed
echo.
echo [X] Echec creation .venv. Verifie que Python est correctement installe.
pause
exit /b 1

:deps_failed
echo.
echo [X] Echec installation des dependances.
echo Reessaie en lancant manuellement : pip install -r requirements.txt
pause
exit /b 1

:config_failed
echo.
echo [X] Configuration annulee ou echec.
pause
exit /b 1

:end
echo.
echo ===============================================================
echo   FIN DE L'EXECUTION
echo ===============================================================
echo.
pause
