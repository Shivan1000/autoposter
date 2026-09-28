@echo off
setlocal enabledelayedexpansion
title YouTube Downloader (1080p / Highest Quality)
color 0B

:: Check for yt-dlp
where yt-dlp >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] 'yt-dlp' is not found in your system PATH.
    echo Please install yt-dlp or add it to PATH.
    echo.
    pause
    exit /b
)

:: Set Default Download Directory to D:\Downloads (creates it if missing)
set "DOWNLOAD_DIR=D:\Downloads\YT downloads"
if not exist "%DOWNLOAD_DIR%" (
    mkdir "%DOWNLOAD_DIR%" 2>nul
)

:menu
cls
echo ===================================================================
echo                     YOUTUBE VIDEO DOWNLOADER
echo ===================================================================
echo  - Saves videos to: %DOWNLOAD_DIR%
echo  - Resolution: 1080p Full HD (or highest available quality)
echo  - Format: MP4 (Video + Best Audio merged)
echo ===================================================================
echo.

set "VIDEO_URL="
set /p "VIDEO_URL=Paste YouTube URL (or type 'exit' to quit): "

:: Remove surrounding quotes if user pastes with quotes
set "VIDEO_URL=%VIDEO_URL:"=%"
:: Strip leading yt-dlp if user pasted as a command
set "VIDEO_URL=%VIDEO_URL:yt-dlp =%"

if /i "%VIDEO_URL%"=="exit" goto end
if "%VIDEO_URL%"=="" (
    echo.
    echo [!] No URL provided. Please enter a valid URL.
    timeout /t 2 >nul
    goto menu
)

echo.
echo [INFO] Fetching video information and downloading in 1080p / best quality...
echo -------------------------------------------------------------------

yt-dlp -f "bv*[height=1080]+ba/b[height=1080]/bv*+ba/b" --merge-output-format mp4 --progress --no-mtime -o "%DOWNLOAD_DIR%\%%(title)s.%%(ext)s" "%VIDEO_URL%"

if %ERRORLEVEL% equ 0 (
    echo -------------------------------------------------------------------
    echo [SUCCESS] Video downloaded successfully to:
    echo %DOWNLOAD_DIR%
) else (
    echo -------------------------------------------------------------------
    echo [ERROR] Download encountered an issue.
)

echo -------------------------------------------------------------------
echo.
set "CHOICE="
set /p "CHOICE=Download another video? (Y/N, or press O to open Downloads): "

if /i "%CHOICE%"=="O" (
    explorer "%DOWNLOAD_DIR%"
    goto menu
)
if /i "%CHOICE%"=="Y" goto menu

:end
echo.
echo Thank you for using YouTube Downloader!
timeout /t 2 >nul
exit /b
