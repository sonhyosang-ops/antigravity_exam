# 🎓 AI 지능형 시험 자동 채점 & 통계 리포트 시스템

선생님을 위한 AI 기반 시험 채점 및 성적 관리 시스템입니다.  
교사가 작성한 **[1. 문제지]**, **[2. 답안지 양식]**, **[3. 정답 및 배점기준표]** 파일과 **[학생 답안지 스캔본]**을 업로드하면, **Upstage Document AI(OCR + Solar LLM)**가 3대 문서를 교차 분석하여 정확한 기준표를 세우고, 손글씨 답안을 자동 채점한 뒤, **지정된 구글 스프레드시트 서식에 맞춰 학과별 시트가 자동 분할된 엑셀 파일**을 완성해 줍니다.

---

## 🔒 보안 강화 조치 (Security First)

- **API 키 화면 노출 원천 차단**: 웹 브라우저 화면 어디에도 API 키 입력창이나 키 텍스트가 표시되지 않습니다.
- **로컬 보안 파일 관리**: 오직 로컬 환경 설정 파일([.env](file:///Users/hyosangson/antigravity_exam/.env))에서만 백그라운드로 안전하게 읽어옵니다.
- **Git 커밋 차단**: [.gitignore](file:///Users/hyosangson/antigravity_exam/.gitignore)에 `.env` 및 `secrets.toml`이 등록되어 있어 실수로 외부 저장소에 유출되지 않습니다.

---

## ✨ 핵심 기능

1. **3대 시험 자료 교차 융합 분석 (Step 1)**
   - **📄 문제지 파일** (PDF/이미지): 지문, 문제 본문, 문항 목록 파악
   - **📝 답안지 양식 파일** (PDF/이미지): 답안 작성란 위치, 서술형 박스 형태 파악
   - **🎯 정답 및 배점표 파일** (PDF/이미지/TXT): 정답, 배점(점수), 서술형 루브릭 파악
   - AI가 3개 문서를 대조하여 소문항(`1-1, 1-2, 6-1, 6-2` 등), 배점, 정답을 완전 동적으로 추출하며, 교사가 화면에서 즉시 수정할 수 있습니다.
2. **학생 손글씨 필기 답안 정밀 OCR (Step 2)**
   - 학생 답안지 상단의 **학과, 학번, 성명** 및 각 문항별 필기 답안을 Upstage OCR로 정밀 추출합니다.
3. **지능형 하이브리드 채점**
   - **객관식/단답형**: 일치 여부 판정 (만점 / 0점)
   - **서술형**: 핵심 루브릭 및 키워드 기반 AI 부분점수 판정 (`O` 만점, `△` 부분정답, `X` 오답)
4. **구글 스프레드시트 규격 완벽 일치 & 학과별 자동 시트 분할 (Step 3 & 4)**
   - **학과별 시트 자동 생성**: 학생 답안지의 학과명(예: `기계공학과`, `전자과`, `소프트웨어과`)을 기준으로 엑셀 내 탭이 자동으로 분리 생성됩니다.
   - **3행 헤더 구조**:
     - 1행: `문항, 1-1, 1-2, 2, ..., 득점, 순위`
     - 2행: `정답, ..., 총배점, `
     - 3행: `학번, 성명, ...`
     - 4행~: 학생별 취득 점수 + 자동 수식 (`=SUM(...)`, `=RANK(...)`)

---

## 🚀 빠른 시작 가이드

### 1. 환경 설정 및 라이브러리 설치
```bash
pip install -r requirements.txt
```

### 2. Upstage API Key 등록 (보안 파일)
프로젝트 폴더 내의 [.env](file:///Users/hyosangson/antigravity_exam/.env) 파일을 열고 실제 Upstage API Key를 입력합니다:

```bash
UPSTAGE_API_KEY=실제_발급받은_키_입력
```
> ※ 키는 화면에 노출되지 않으며, [Upstage 콘솔](https://console.upstage.ai)에서 발급받으실 수 있습니다.

### 3. 프로그램 실행
```bash
streamlit run app.py
```
브라우저(`http://localhost:8501`)에서 교사용 웹 화면이 바로 열립니다.

---

## 📂 파일 구조

- [app.py](file:///Users/hyosangson/antigravity_exam/app.py): 보안 강화 교사용 Streamlit 대시보드
- [criteria_parser.py](file:///Users/hyosangson/antigravity_exam/criteria_parser.py): 3대 시험 문서(문제/답안지/정답지) 융합 분석 파서
- [ocr_grader.py](file:///Users/hyosangson/antigravity_exam/ocr_grader.py): 학생 답안지 OCR 인식 및 정오/부분점수 채점 엔진
- [report_generator.py](file:///Users/hyosangson/antigravity_exam/report_generator.py): 구글 스프레드시트 양식 100% 일치 학과별 시트 분할 엑셀 생성기
- [upstage_client.py](file:///Users/hyosangson/antigravity_exam/upstage_client.py): 보안 강화 Upstage Document AI 및 Solar LLM 클라이언트
- [.env](file:///Users/hyosangson/antigravity_exam/.env): API Key 보안 저장 파일 (Git 제외)
- [.gitignore](file:///Users/hyosangson/antigravity_exam/.gitignore): 보안 파일 커밋 차단 설정
