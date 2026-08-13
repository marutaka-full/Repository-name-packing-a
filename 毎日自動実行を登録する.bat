@echo off
chcp 932 > nul
cd /d "%~dp0"

if not exist "law_text_sync.py" (
  echo.
  echo  ★ ここは C:\法令監視 ではありません。
  echo     正しいフォルダの中から実行してください。
  echo.
  pause > nul
  exit /b 1
)

echo ============================================================
echo  毎日の自動実行を登録します
echo ============================================================
echo.
echo  対象フォルダ: %~dp0
echo.
echo  ・パソコンの電源が入っている必要があります
echo  ・失敗した日はデスクトップに「★法令監視_要確認.txt」が出ます
echo.

set "HH="
set /p HH=何時に実行しますか（0～23の数字・例 7）: 
if "%HH%"=="" set HH=7

rem 古い登録を消してから入れ直す
schtasks /delete /tn "法令監視_条文更新" /f > nul 2>&1

schtasks /create /tn "法令監視_条文更新" /tr "%~dp0_自動実行の中身.bat" /sc daily /st %HH%:00 /f
if errorlevel 1 goto FAIL

echo.
echo ============================================================
echo  [完了] 毎日 %HH%:00 に自動実行されます。
echo ============================================================
echo.
echo  今すぐテスト実行します。1～3分かかります。
echo  Enter を押してください。
pause > nul
schtasks /run /tn "法令監視_条文更新"
echo.
echo  実行中です。3分ほど待ってから、このフォルダに
echo  monitor.log ができているか確認してください。
echo.
pause > nul
exit /b 0

:FAIL
echo.
echo  登録できませんでした。
echo  このファイルを右クリック →「管理者として実行」でやり直してください。
echo.
pause > nul
exit /b 1
