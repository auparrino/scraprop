@echo off
REM Abre el visor en el navegador. Sirve la carpeta viewer\ por http://localhost:8765/
cd /d "%~dp0\viewer"
start "" http://localhost:8765/
python -m http.server 8765
