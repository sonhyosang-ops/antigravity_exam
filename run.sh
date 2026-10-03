#!/bin/bash
echo "========================================================"
echo "  AI 지능형 시험 자동 채점 시스템 실행 중..."
echo "========================================================"

# 1. 파이썬 확인
if ! command -v python3 &> /dev/null; then
    echo "[오류] python3이 설치되어 있지 않습니다."
    exit 1
fi

# 2. 필수 라이브러리 설치
echo "[1/2] 필수 라이브러리 확인 및 설치 중..."
pip3 install -r requirements.txt -q

# 3. 스트림릿 실행
echo "[2/2] 스트림릿 웹 대시보드를 실행합니다..."
streamlit run app.py
