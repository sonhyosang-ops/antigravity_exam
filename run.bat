@echo off
chcp 65001 > nul
echo ========================================================
echo   AI 지능형 시험 자동 채점 시스템 실행 중...
echo ========================================================

:: 1. 파이썬 설치 확인
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo [오류] 파이썬(Python)이 설치되어 있지 않습니다.
    echo https://www.python.org 에서 파이썬을 설치해주세요.
    pause
    exit /b
)

:: 2. 환경 설정 파일(.env) 확인
if not exist .env (
    if exist .env.example (
        copy .env.example .env > nul
        echo [.env 생성] .env 파일이 자동 생성되었습니다.
        echo 메모장으로 .env 파일을 열어 실제 UPSTAGE_API_KEY를 입력해주세요!
    )
)

:: 2. 필수 라이브러리 자동 설치
echo [1/2] 필수 라이브러리 확인 및 설치 중...
pip install -r requirements.txt -q

:: 3. 스트림릿 웹 앱 실행
echo [2/2] 스트림릿 웹 대시보드를 실행합니다...
echo 브라우저 창이 자동으로 열립니다. (종료하려면 이 창에서 Ctrl+C를 누르세요)
streamlit run app.py

pause
