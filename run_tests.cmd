@echo off
rem Run the test-suite from this checkout without installing anything.  Usage: run_tests [options]  (see run_tests.py --help)
setlocal
where py >nul 2>nul
if %ERRORLEVEL%==0 (set "PTW_PY=py -3") else (set "PTW_PY=python")
%PTW_PY% "%~dp0run_tests.py" %*
exit /b %ERRORLEVEL%
