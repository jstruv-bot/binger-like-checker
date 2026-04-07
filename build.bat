@echo off
echo ============================================
echo   Binger Like Checker - Build Script
echo ============================================
echo.

echo Installing dependencies...
pip install requests pyinstaller
echo.

echo Building .exe ...
pyinstaller --onefile --windowed --name "BingerLikeChecker" like_checker.py
echo.

echo ============================================
if exist "dist\BingerLikeChecker.exe" (
    echo BUILD SUCCESSFUL!
    echo .exe is at: dist\BingerLikeChecker.exe
) else (
    echo BUILD FAILED - check errors above
)
echo ============================================
pause
