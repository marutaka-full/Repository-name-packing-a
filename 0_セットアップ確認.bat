@echo off
chcp 932 > nul
cd /d "%~dp0"
echo ============================================================
echo  セットアップ確認（テスト機用）
echo ============================================================
echo.

echo [1] Python の確認
python --version 2>nul
if errorlevel 1 (
  echo   python コマンドが見つかりません。py を試します。
  py --version 2>nul
  if errorlevel 1 (
    echo.
    echo   ★ Python が入っていません。
    echo      python.org からインストールし、
    echo      「Add python.exe to PATH」に必ずチェックを入れてください。
    echo.
    pause > nul
    exit /b 1
  )
)
echo.

echo [2] 自己検証（ネット接続不要）
echo --- law_text_sync  期待値 10/10 ---
python law_text_sync.py --selftest
echo --- monitor_layer1  期待値 9/9 ---
python monitor_layer1.py --selftest
echo --- monitor_layer235 期待値 7/7 ---
python monitor_layer235.py --selftest
echo.

echo [3] e-Gov 法令API への接続確認
python -c "import urllib.request;r=urllib.request.urlopen('https://laws.e-gov.go.jp/api/2/law_data/428AC0000000089',timeout=20);print('  接続OK  HTTP',r.status)" 2>nul
if errorlevel 1 (
  echo   ★ 接続できませんでした。
  echo      社内プロキシ／ファイアウォールの可能性があります。
  echo      laws.e-gov.go.jp への通信許可を情報担当に確認してください。
)
echo.

echo ============================================================
echo  確認終了。上の PASS 数が 10/10・9/9・7/7 で、
echo  [3] が「接続OK」なら導入完了です。
echo  何かキーを押すと閉じます。
echo ============================================================
pause > nul
