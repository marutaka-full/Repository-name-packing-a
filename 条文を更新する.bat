@echo off
chcp 932 > nul
cd /d "%~dp0"

rem --- 使う Python を決める ---
set PYEXE=python
python --version > nul 2>&1
if errorlevel 1 (
  set PYEXE=py
  py --version > nul 2>&1
  if errorlevel 1 (
    echo.
    echo  ★ Python が見つかりません。
    echo     python.org から導入し「Add python.exe to PATH」に
    echo     チェックを入れて再インストールしてください。
    echo.
    pause > nul
    exit /b 9
  )
)

echo ============================================================
echo  法令条文の更新を開始します
echo  (このウィンドウは終了まで閉じないでください)
echo ============================================================
echo.

%PYEXE% law_text_sync.py
set RC=%errorlevel%

echo.
if "%RC%"=="3" goto FETCHFAIL
if not "%RC%"=="0" goto OTHERERR

echo ============================================================
echo  [正常終了] 全法令の確認が完了しました。
echo  変更があった場合は diff_report_日付.txt を確認してください。
echo ============================================================
goto END

:FETCHFAIL
echo ############################################################
echo  [要確認] 取得できなかった法令があります。
echo.
echo  上に表示された「×」の法令は "改正なし" ではなく
echo  "未確認" です。この日の監視は完了していません。
echo.
echo  ネットワーク接続とプロキシ設定を確認し、
echo  復旧後にもう一度このバッチを実行してください。
echo ############################################################
goto END

:OTHERERR
echo ############################################################
echo  [異常終了] エラーコード %RC%
echo  上の表示内容を控えて担当者に連絡してください。
echo ############################################################
goto END

:END
echo.
echo  何かキーを押すと閉じます。
pause > nul
exit /b %RC%
