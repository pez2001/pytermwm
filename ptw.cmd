@echo off
rem Run pytermwm from this checkout without installing it.  Usage: ptw [pytermwm arguments]
setlocal
where py >nul 2>nul
if %ERRORLEVEL%==0 (set "PTW_PY=py -3") else (set "PTW_PY=python")
%PTW_PY% "%~dp0ptw.py" %*
exit /b %ERRORLEVEL%
