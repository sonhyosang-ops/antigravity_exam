import os
import io
import json
import streamlit as st
import pandas as pd
from dotenv import load_dotenv

# 로컬 모듈 임포트
from upstage_client import UpstageClient, get_secret_api_key
from criteria_parser import CriteriaParser
from ocr_grader import StudentGrader
from report_generator import ExcelReportGenerator

# .env 로드
load_dotenv(override=True)

# 페이지 설정
st.set_page_config(
    page_title="AI 시험 자동 채점 & 통계 리포트 시스템",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 보안 강화 및 프리미엄 교사용 UI 스타일링
st.markdown("""
<style>
    .main-header {
        font-size: 26px;
        font-weight: 700;
        color: #1E293B;
        margin-bottom: 4px;
    }
    .sub-header {
        font-size: 14px;
        color: #64748B;
        margin-bottom: 24px;
    }
    .step-card {
        background-color: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-radius: 12px;
        padding: 24px;
        margin-bottom: 24px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    .step-title {
        font-size: 18px;
        font-weight: 600;
        color: #0F172A;
        margin-bottom: 12px;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .badge {
        background-color: #2563EB;
        color: white;
        padding: 3px 10px;
        border-radius: 6px;
        font-size: 13px;
        font-weight: 600;
    }
    .upload-box {
        background-color: #F8FAFC;
        border: 1px dashed #CBD5E1;
        border-radius: 8px;
        padding: 12px;
        min-height: 140px;
    }
    .secure-badge {
        background-color: #ECFDF5;
        border: 1px solid #A7F3D0;
        color: #065F46;
        padding: 8px 12px;
        border-radius: 8px;
        font-size: 13px;
        font-weight: 500;
        margin-bottom: 16px;
    }
</style>
""", unsafe_allow_html=True)

# 세션 상태 초기화
if "criteria_data" not in st.session_state:
    st.session_state.criteria_data = None
if "criteria_df" not in st.session_state:
    st.session_state.criteria_df = None
if "criteria_version" not in st.session_state:
    st.session_state.criteria_version = 0
if "graded_students" not in st.session_state:
    st.session_state.graded_students = []
if "excel_data" not in st.session_state:
    st.session_state.excel_data = None

# 보안 키 확인 (화면에 절대 키를 입력받지 않음)
load_dotenv(override=True)
secret_key = get_secret_api_key()
has_valid_key = bool(secret_key and secret_key != "your_upstage_api_key_here")


# ----------------- 사이드바 설정 -----------------
with st.sidebar:
    snue_logo_path = os.path.join(os.path.dirname(__file__), "assets", "snue_logo.png")
    if os.path.exists(snue_logo_path):
        st.image(snue_logo_path, use_container_width=True)
    else:
        st.markdown("### 🎓 **서울교육대학교**")
    st.markdown("### 🔒 보안 인증 상태")
    
    if has_valid_key:
        st.markdown("""
        <div class="secure-badge">
            🛡️ <b>보안 인증 완료</b><br/>
            API Key가 환경 설정 파일(<code>.env</code>)에서 안전하게 로드되었습니다.
        </div>
        """, unsafe_allow_html=True)
    else:
        st.error("⚠️ API 키가 설정되지 않았습니다.")
        st.info("보안 강화를 위해 화면 대신 프로젝트 폴더의 `.env` 파일에 `UPSTAGE_API_KEY=...` 형태로 키를 저장해 주세요.")

    st.markdown("---")
    st.markdown("### 🔄 시험 세션 관리")
    if st.button("🗑️ 새 시험 시작 (데이터 초기화)", use_container_width=True):
        st.session_state.criteria_data = None
        st.session_state.criteria_df = None
        st.session_state.graded_students = []
        st.session_state.excel_data = None
        st.success("데이터가 초기화되었습니다. 새로운 시험 자료를 업로드해주세요.")
        st.rerun()

    st.markdown("---")
    st.markdown("""
    **💡 시험 운영 가이드**
    1. **교사 파일 업로드**: 문제지, 답안지 양식, 정답 및 배점표
    2. **AI 동적 분석**: 문항 체계(`1-1, 1-2..`), 배점 자동 추출
    3. **학생 답안 채점**: 손글씨 OCR 및 지능형 채점
    4. **엑셀 리포트**: 학과별 시트 자동 분할 엑셀 다운로드
    """)

# ----------------- 메인 헤더 -----------------
st.markdown('<div class="main-header">🎓 AI 지능형 시험 자동 채점 & 통계 리포트 시스템</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">문제지, 답안지, 정답지 3대 문서를 교차 분석하여 정확한 기준표를 세우고, 학과별 맞춤 엑셀 성적표를 산출합니다.</div>', unsafe_allow_html=True)

if not has_valid_key:
    st.warning("⚠️ 시스템을 사용하려면 프로젝트 루트의 `.env` 파일에 Upstage API Key를 저장해야 합니다.")
    st.code("UPSTAGE_API_KEY=실제_업스테이지_API_키", language="bash")
    st.stop()

# 클라이언트 및 파서 인스턴스화
client = UpstageClient(api_key=secret_key)
criteria_parser = CriteriaParser(client)
grader = StudentGrader(client)


# ========================================================
# STEP 1: 교사 시험 자료 3대 파일 업로드 및 동적 기준표 생성
# ========================================================
st.markdown('<div class="step-card">', unsafe_allow_html=True)
st.markdown('<div class="step-title"><span class="badge">Step 1</span> 교사 시험 자료 업로드 (문제지 · 답안지 · 정답지)</div>', unsafe_allow_html=True)
st.markdown("새로운 시험을 진행할 때마다 해당 시험의 <b>[1. 문제지]</b>, <b>[2. 답안지 양식]</b>, <b>[3. 정답 및 배점기준표]</b>를 각각 업로드하세요. AI가 세 문서를 대조하여 문항 번호와 배점을 완벽히 파악합니다.", unsafe_allow_html=True)

col1, col2, col3 = st.columns(3)

with col1:
    st.markdown("##### 📄 1. 문제지 파일")
    q_file = st.file_uploader(
        "문제지 업로드",
        type=["pdf", "png", "jpg", "jpeg"],
        key="uploader_question",
        help="문제 본문 및 지문이 포함된 파일입니다."
    )

with col2:
    st.markdown("##### 📝 2. 답안지 파일 (양식)")
    t_file = st.file_uploader(
        "답안지 양식 업로드",
        type=["pdf", "png", "jpg", "jpeg"],
        key="uploader_template",
        help="학생들이 답을 작성하는 빈 답안용지 서식 파일입니다."
    )

with col3:
    st.markdown("##### 🎯 3. 정답 및 배점기준표")
    a_file = st.file_uploader(
        "정답/배점표 업로드",
        type=["pdf", "png", "jpg", "jpeg", "xlsx", "xls", "csv", "txt"],
        key="uploader_answer_key",
        help="각 문항별 정답, 배점, 부분점수 기준이 적힌 파일입니다. (PDF, 이미지, 엑셀 가능)"
    )

can_parse = (q_file is not None) or (t_file is not None) or (a_file is not None)

st.markdown("<br/>", unsafe_allow_html=True)
c_btn, c_stat = st.columns([1, 2])
with c_btn:
    parse_trigger = st.button(
        "🔍 3대 문서 융합 AI 기준표 생성",
        type="primary",
        disabled=not can_parse,
        use_container_width=True
    )
with c_stat:
    if can_parse and st.session_state.criteria_df is None:
        st.info("💡 파일 업로드가 감지되었습니다. 왼쪽의 **[🔍 3대 문서 융합 AI 기준표 생성]** 버튼을 클릭하세요!")

if parse_trigger:
    with st.spinner("Upstage Document AI가 문제지, 답안지 서식, 정답/배점표를 교차 분석하여 통합 채점 기준표를 구성 중입니다..."):
        try:
            # 엑셀 또는 CSV 파일인 경우 직접 스마트 파싱 지원
            if a_file and a_file.name.lower().endswith(('.xlsx', '.xls', '.csv')):
                try:
                    if a_file.name.lower().endswith('.csv'):
                        raw_df = pd.read_csv(a_file)
                    else:
                        raw_df = pd.read_excel(a_file)
                    # 헤더 매핑 시도
                    items = []
                    for idx, r in raw_df.iterrows():
                        row_vals = [str(v) for v in r.values if pd.notna(v)]
                        if row_vals:
                            items.append({
                                "item_no": str(r.get("문항번호", r.get("문항", idx+1))),
                                "score": float(r.get("배점", 4.0)),
                                "type": str(r.get("유형", "단답형")),
                                "correct_answer": str(r.get("정답", row_vals[-1])),
                                "rubric": str(r.get("채점기준", "정답 일치"))
                            })
                    if items:
                        parsed_criteria = {"exam_title": "엑셀 기반 시험", "total_score": sum(i["score"] for i in items), "items": items}
                    else:
                        raise ValueError("엑셀에서 문항을 찾지 못함")
                except Exception:
                    # 엑셀 직접 읽기 실패 시 Upstage로 폴백
                    a_file.seek(0)
                    parsed_criteria = criteria_parser.parse_three_materials(
                        answer_key_content=a_file.read(),
                        answer_key_name=a_file.name
                    )
            else:
                q_bytes = q_file.read() if q_file else None
                q_name = q_file.name if q_file else "question.pdf"
                
                t_bytes = t_file.read() if t_file else None
                t_name = t_file.name if t_file else "template.pdf"
                
                a_bytes = a_file.read() if a_file else None
                a_name = a_file.name if a_file else "answer_key.pdf"

                parsed_criteria = criteria_parser.parse_three_materials(
                    question_content=q_bytes,
                    question_name=q_name,
                    template_content=t_bytes,
                    template_name=t_name,
                    answer_key_content=a_bytes,
                    answer_key_name=a_name
                )
            
            st.session_state.criteria_data = parsed_criteria
            st.session_state.criteria_df = CriteriaParser.to_dataframe(parsed_criteria)
            st.session_state.criteria_version += 1
            st.success("✅ 문제지/답안지/정답지 융합 분석 완료! 채점 기준표가 생성되었습니다.")
            st.rerun()
        except Exception as e:
            st.error(f"기준표 파싱 중 오류가 발생했습니다: {str(e)}")

# 파싱된 기준표 확인 및 편집
if st.session_state.criteria_df is not None:
    st.markdown("---")
    st.markdown("#### 📋 생성된 채점 기준표 검토 및 편집")
    st.caption("소문항(`1-1, 1-2, 2..`), 배점, 정답, 채점기준이 정확한지 확인하세요. 표 안의 셀을 직접 클릭하여 수정하거나 행을 추가/삭제할 수 있습니다.")
    
    editor_key = f"criteria_editor_v{st.session_state.criteria_version}"
    edited_df = st.data_editor(
        st.session_state.criteria_df,
        num_rows="dynamic",
        use_container_width=True,
        key=editor_key
    )
    
    # 수정 내용 동기화
    st.session_state.criteria_data = CriteriaParser.from_dataframe(edited_df)
    
    total_pts = sum(float(item["score"]) for item in st.session_state.criteria_data.get("items", []))
    item_cnt = len(st.session_state.criteria_data.get("items", []))
    st.info(f"📌 총 문항 수: **{item_cnt}개** (세부 평가 단위) | 총 배점 합계: **{total_pts:.1f}점**")

st.markdown('</div>', unsafe_allow_html=True)


# ========================================================
# STEP 2: 학생 답안지 업로드 및 자동 채점
# ========================================================
if st.session_state.criteria_data and len(st.session_state.criteria_data.get("items", [])) > 0:
    st.markdown('<div class="step-card">', unsafe_allow_html=True)
    st.markdown('<div class="step-title"><span class="badge">Step 2</span> 학생 답안지 업로드 및 자동 채점</div>', unsafe_allow_html=True)
    st.markdown("스캔된 학생 답안지 파일들을 일괄 업로드하세요. 답안지 상단의 **학과, 학번, 성명**과 **문항별 기입 답안**을 인식하여 자동 채점합니다.")
    
    col_a, col_b = st.columns([2, 1])
    with col_a:
        student_files = st.file_uploader(
            "학생 답안지 스캔 파일들 업로드 (PDF 또는 이미지 다중 선택)",
            type=["pdf", "png", "jpg", "jpeg"],
            accept_multiple_files=True,
            key="student_uploader_step2"
        )
    with col_b:
        default_dept = st.text_input(
            "기본 학과명 (답안지에 학과 미기재 시 적용)",
            value="기계공학과",
            help="답안지 상단에 학과명이 명시되지 않은 경우 이 학과명으로 자동 배정됩니다."
        )

    if student_files:
        st.write(f"📁 업로드된 답안지: **총 {len(student_files)}부**")
        start_grade_btn = st.button("🚀 전체 학생 자동 채점 시작", type="primary", use_container_width=True)
        
        if start_grade_btn:
            progress_bar = st.progress(0)
            status_text = st.empty()
            graded_list = []
            
            for i, s_file in enumerate(student_files):
                status_text.text(f"[{i+1}/{len(student_files)}] '{s_file.name}' 손글씨 답안 인식 및 채점 진행 중...")
                try:
                    s_bytes = s_file.read()
                    # 1. OCR 인식 및 학생 정보/답안 추출
                    student_info = grader.parse_student_sheet(
                        file_content=s_bytes,
                        filename=s_file.name,
                        criteria_data=st.session_state.criteria_data,
                        default_department=default_dept
                    )
                    # 2. 채점 실행
                    graded_res = grader.grade_student(student_info, st.session_state.criteria_data)
                    graded_list.append(graded_res)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    st.error(f"'{s_file.name}' 처리 실패: {str(e)}")
                    
                progress_bar.progress((i + 1) / len(student_files))
                
            if graded_list:
                status_text.text(f"🎉 총 {len(graded_list)}명의 학생 답안 채점이 완료되었습니다!")
                st.session_state.graded_students = graded_list
                with st.spinner("구글 스프레드시트 양식으로 학과별 시트 분할 엑셀을 생성 중입니다..."):
                    excel_bytes = ExcelReportGenerator.generate_excel_report(
                        criteria_data=st.session_state.criteria_data,
                        graded_students=graded_list
                    )
                    st.session_state.excel_data = excel_bytes
                st.rerun()
            else:
                status_text.text("⚠️ 처리 완료된 학생 답안지가 없습니다. 파일 형식과 내용을 확인해주세요.")
                st.error("채점된 학생 데이터가 없습니다. 업로드된 파일이 올바른 답안지 이미지나 PDF인지 확인해주세요.")

    st.markdown('</div>', unsafe_allow_html=True)


# ========================================================
# STEP 3 & 4: 채점 결과 조회 및 엑셀 다운로드
# ========================================================
if st.session_state.graded_students:
    st.markdown('<div class="step-card">', unsafe_allow_html=True)
    st.markdown('<div class="step-title"><span class="badge">Step 3 & 4</span> 학과별 채점 결과표 및 엑셀 다운로드</div>', unsafe_allow_html=True)
    
    # 엑셀 다운로드 버튼 (성적표 + 상세 피드백 종합)
    if st.session_state.excel_data:
        dl_col_main1, dl_col_main2 = st.columns(2)
        with dl_col_main1:
            st.download_button(
                label="📥 구글 스프레드시트 맞춤형 엑셀 다운로드 (.xlsx)",
                data=st.session_state.excel_data,
                file_name="채점_결과표.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True,
                help="구글 스프레드시트 규격 3행 헤더 및 학과별 분할 시트가 적용된 성적표입니다."
            )
        with dl_col_main2:
            all_fb_bytes = ExcelReportGenerator.generate_all_feedback_report(
                criteria_data=st.session_state.criteria_data,
                graded_students=st.session_state.graded_students
            )
            st.download_button(
                label="📊 전체 학생 상세 피드백 & 정오표 종합 (.xlsx)",
                data=all_fb_bytes,
                file_name="전체학생_채점피드백_종합정오표.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                help="전체 학생의 문항별 정답, 학생답안, 정오, 득점, AI 및 수기 검토 피드백이 담긴 종합 리포트입니다."
            )
    st.markdown("---")

    # 학과별 그룹핑 및 탭 생성
    dept_groups = {}
    for student in st.session_state.graded_students:
        dept = student.get("department", "일반")
        if dept not in dept_groups:
            dept_groups[dept] = []
        dept_groups[dept].append(student)

    st.markdown("#### 🏫 학과별 성적표 (시트 분할 현황)")
    tab_titles = [f"📂 {dept} ({len(students)}명)" for dept, students in dept_groups.items()]
    tabs = st.tabs(tab_titles)

    items = st.session_state.criteria_data.get("items", [])
    item_keys = [item["item_no"] for item in items]

    for (dept_name, students), tab in zip(dept_groups.items(), tabs):
        with tab:
            # 학과 요약 통계
            scores = [s.get("total_score", 0.0) for s in students]
            avg_score = sum(scores) / len(scores) if scores else 0
            max_score = max(scores) if scores else 0
            min_score = min(scores) if scores else 0
            
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("응시 인원", f"{len(students)}명")
            m2.metric("학과 평균", f"{avg_score:.1f}점")
            m3.metric("최고점", f"{max_score:.1f}점")
            m4.metric("최저점", f"{min_score:.1f}점")
            
            # 구글 스프레드시트 구조 미리보기
            table_rows = []
            for s in sorted(students, key=lambda x: x.get("total_score", 0), reverse=True):
                r = {
                    "학번": s.get("student_id", ""),
                    "성명": s.get("name", "")
                }
                for k in item_keys:
                    r[k] = s.get("results", {}).get(k, {}).get("score", 0.0)
                r["득점"] = s.get("total_score", 0.0)
                table_rows.append(r)
                
            preview_df = pd.DataFrame(table_rows)
            st.dataframe(preview_df, use_container_width=True)

            # 학생별 상세 답안 및 정오표 확인
            with st.expander(f"🔍 {dept_name} 학생별 작성 답안 및 정오표(O/△/X) 상세 보기"):
                for s in sorted(students, key=lambda x: x.get("total_score", 0), reverse=True):
                    st.markdown(f"**[{s.get('student_id')}] {s.get('name')}** (총점: {s.get('total_score')}점)")
                    uncertain_items = s.get("uncertain", [])
                    if uncertain_items:
                        st.warning(f"⚠️ **수기 검토 권장 (판독 불명확 항목):** {', '.join([str(u) for u in uncertain_items])}")
                    detail_rows = []
                    for k in item_keys:
                        res = s.get("results", {}).get(k, {})
                        detail_rows.append({
                            "문항": k,
                            "학생 답안": res.get("answer", ""),
                            "정오": res.get("status", "X"),
                            "득점": res.get("score", 0.0),
                            "채점 피드백": res.get("feedback", "")
                        })
                    st.table(pd.DataFrame(detail_rows))
                    st.markdown("---")

    # ----------------------------------------------------
    # 수기 검토 및 채점자 최종 정오/득점 확정 UI
    # ----------------------------------------------------
    st.markdown("---")
    st.markdown("#### ✏️ 수기 검토 및 채점자 최종 정오/득점 확정")
    st.caption("AI 채점 결과 중 서술형/단답형의 오판이 있는 경우, 아래 표에서 **[정오(O/△/X)]** 또는 **[득점]**을 직접 수정하여 채점자의 판단을 **최종 정답/오답으로 확정**할 수 있습니다. 확정 시 총점과 엑셀 리포트가 즉시 재계산됩니다.")

    # 전체 학생 중 검토 필요 학생 파악
    student_labels = []
    student_map = {}
    for idx, s in enumerate(st.session_state.graded_students):
        s_id = s.get("student_id", f"idx_{idx}")
        s_name = s.get("name", "무명")
        s_dept = s.get("department", "일반")
        s_tot = s.get("total_score", 0.0)
        has_review = bool(s.get("uncertain")) or any(r.get("needs_review") for r in s.get("results", {}).values())
        badge = " ⚠️ [수기검토 필요]" if has_review else ""
        label = f"{s_dept} | {s_id} {s_name} (총점: {s_tot:.1f}점){badge}"
        student_labels.append(label)
        student_map[label] = idx

    selected_label = st.selectbox("검토/수정할 학생을 선택하세요:", student_labels)
    selected_idx = student_map[selected_label]
    target_student = st.session_state.graded_students[selected_idx]

    # 안내 메시지
    if target_student.get("uncertain"):
        st.warning(f"⚠️ **수기 검토 권장 항목:** {', '.join([str(u) for u in target_student.get('uncertain', [])])} — 아래 표에서 정오와 득점을 직접 확인/확정해주세요.")

    # 기준표 정보 매핑
    crit_map = {item["item_no"]: item for item in st.session_state.criteria_data.get("items", [])}

    edit_rows = []
    for k in item_keys:
        res = target_student.get("results", {}).get(k, {})
        crit = crit_map.get(k, {})
        edit_rows.append({
            "문항": k,
            "학생 답안": str(res.get("answer", "")),
            "기준 정답": str(crit.get("correct_answer", "")),
            "유형": str(crit.get("type", "단답형")),
            "배점": float(crit.get("score", 0.0)),
            "정오": str(res.get("status", "X")),
            "득점": float(res.get("score", 0.0)),
            "채점 피드백": str(res.get("feedback", ""))
        })

    edit_df = pd.DataFrame(edit_rows)

    editor_key = f"student_edit_{selected_idx}_{target_student.get('total_score', 0)}"
    edited_student_df = st.data_editor(
        edit_df,
        column_config={
            "문항": st.column_config.TextColumn(disabled=True),
            "학생 답안": st.column_config.TextColumn(help="학생 답안 원문을 수정할 수 있습니다."),
            "기준 정답": st.column_config.TextColumn(disabled=True),
            "유형": st.column_config.TextColumn(disabled=True),
            "배점": st.column_config.NumberColumn(disabled=True, format="%.1f"),
            "정오": st.column_config.SelectboxColumn(
                options=["O", "△", "X"],
                required=True,
                help="채점자가 직접 O(정답), △(부분점수), X(오답)로 최종 확정할 수 있습니다."
            ),
            "득점": st.column_config.NumberColumn(
                min_value=0.0,
                step=0.5,
                format="%.1f",
                help="채점자가 직접 최종 부여 점수를 입력합니다."
            ),
            "채점 피드백": st.column_config.TextColumn(help="채점 사유를 직접 입력하거나 수정합니다."),
        },
        disabled=["문항", "기준 정답", "유형", "배점"],
        use_container_width=True,
        key=editor_key
    )

    btn_col1, btn_col2 = st.columns(2)

    with btn_col1:
        finalize_btn = st.button(
            "💾 채점자 수정한 정오/득점으로 최종 확정 및 엑셀 재계산",
            type="primary",
            use_container_width=True,
            help="표에서 수정한 정오(O/△/X)와 득점을 최종 점수로 즉시 확정하고 총점과 엑셀 리포트를 재계산합니다."
        )

    with btn_col2:
        ai_regrade_btn = st.button(
            "🔄 [학생 답안] 기준 AI 자동 재채점",
            use_container_width=True,
            help="수정된 학생 답안 텍스트를 바탕으로 AI 채점 엔진에게 다시 채점하도록 요청합니다."
        )

    # 1. 채점자 정오/득점 최종 확정 처리
    if finalize_btn:
        new_results = {}
        new_total = 0.0
        for _, row in edited_student_df.iterrows():
            k = str(row["문항"])
            crit = crit_map.get(k, {})
            max_sc = float(crit.get("score", 0.0))
            
            st_val = str(row["정오"]).upper()
            sc_val = float(row["득점"])
            fb_val = str(row["채점 피드백"]).strip()
            ans_val = str(row["학생 답안"]).strip()

            orig_item = target_student.get("results", {}).get(k, {})
            orig_sc = float(orig_item.get("score", 0.0))
            orig_st = str(orig_item.get("status", "X")).upper()

            # 정오 상태 변경 시 점수 연동 스마트 보정
            if st_val != orig_st:
                if st_val == "X" and sc_val == orig_sc and orig_sc > 0:
                    sc_val = 0.0  # 정오를 X로 변경 시 점수를 0점으로 연동
                elif st_val == "O" and sc_val == orig_sc and orig_sc < max_sc:
                    sc_val = max_sc  # 정오를 O로 변경 시 만점으로 연동

            if not fb_val or fb_val == orig_item.get("feedback"):
                if st_val == "O" and orig_st != "O":
                    fb_val = "채점자 검토 후 최종 정답 확정"
                elif st_val == "X" and orig_st != "X":
                    fb_val = "채점자 검토 후 최종 오답 확정"
                elif st_val == "△" and orig_st != "△":
                    fb_val = f"채점자 검토 후 부분점수 확정 ({sc_val:.1f}점)"

            new_results[k] = {
                "answer": ans_val,
                "score": sc_val,
                "status": st_val,
                "feedback": fb_val,
                "needs_review": False,
                "confirmed_by_human": True
            }
            new_total += sc_val

        # 학생 객체 업데이트
        target_student["results"] = new_results
        target_student["total_score"] = round(new_total, 2)
        target_student["uncertain"] = []
        target_student["review_queue"] = []

        # 엑셀 파일 즉시 재생성
        excel_bytes = ExcelReportGenerator.generate_excel_report(
            criteria_data=st.session_state.criteria_data,
            graded_students=st.session_state.graded_students
        )
        st.session_state.excel_data = excel_bytes

        st.success(f"🎉 [{target_student.get('name')}] 학생의 채점표가 채점자 최종 기준으로 확정되었습니다! (최종 총점: {new_total:.1f}점, 엑셀 동기화 완료)")
        st.rerun()

    # 2. AI 자동 재채점 처리
    if ai_regrade_btn:
        with st.spinner(f"[{target_student.get('name')}] 학생의 수정된 답안을 바탕으로 AI 재채점을 수행 중입니다..."):
            updated_answers = {}
            for _, row in edited_student_df.iterrows():
                k = str(row["문항"])
                ans = str(row["학생 답안"]).strip()
                updated_answers[k] = ans

            student_info_for_regrade = {
                "department": target_student.get("department", "일반"),
                "student_id": target_student.get("student_id", ""),
                "name": target_student.get("name", ""),
                "answers": updated_answers,
                "uncertain": []
            }

            regraded = grader.grade_student(
                student_info=student_info_for_regrade,
                criteria_data=st.session_state.criteria_data
            )

            target_student["results"] = regraded["results"]
            target_student["total_score"] = regraded["total_score"]
            target_student["uncertain"] = []
            target_student["review_queue"] = []

            excel_bytes = ExcelReportGenerator.generate_excel_report(
                criteria_data=st.session_state.criteria_data,
                graded_students=st.session_state.graded_students
            )
            st.session_state.excel_data = excel_bytes

        st.success(f"🎉 [{target_student.get('name')}] 학생의 답안 기준 AI 재채점이 완료되었습니다! (새로운 총점: {regraded['total_score']:.1f}점, 엑셀 파일 동기화 완료)")
        st.rerun()

    # ----------------------------------------------------
    # 수기 검토 및 채점 피드백 다운로드 섹션
    # ----------------------------------------------------
    st.markdown("---")
    st.markdown("##### 📥 수기 검토 및 채점 피드백 자료 다운로드")
    st.caption("채점자가 수기 확정한 정오/득점 및 AI 채점 피드백(오답 사유, 기준 비교 등)이 포함된 상세 리포트를 다운로드할 수 있습니다.")

    single_student_bytes = ExcelReportGenerator.generate_student_feedback_report(
        student=target_student,
        criteria_data=st.session_state.criteria_data
    )
    all_student_bytes = ExcelReportGenerator.generate_all_feedback_report(
        criteria_data=st.session_state.criteria_data,
        graded_students=st.session_state.graded_students
    )

    clean_sname = str(target_student.get('name', '학생')).replace(' ', '_')
    clean_sid = str(target_student.get('student_id', '')).replace(' ', '_')
    clean_dept = str(target_student.get('department', '일반')).replace(' ', '_')

    # CSV 데이터 생성
    csv_bytes = edit_df.to_csv(index=False).encode('utf-8-sig')

    dl_col1, dl_col2, dl_col3 = st.columns(3)

    with dl_col1:
        st.download_button(
            label=f"📥 [{target_student.get('name')}] 피드백 (.xlsx)",
            data=single_student_bytes,
            file_name=f"{clean_dept}_{clean_sid}_{clean_sname}_채점피드백.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True,
            help="선택한 학생의 문항별 정오, 득점, 배점, 채점 피드백, 수기확정 여부가 서식화된 엑셀 리포트입니다."
        )

    with dl_col2:
        st.download_button(
            label="📊 전체 학생 피드백 종합 (.xlsx)",
            data=all_student_bytes,
            file_name="전체학생_채점피드백_종합정오표.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            help="전체 학생의 문항별 상세 피드백 목록 및 수기 확정 내역이 별도 시트로 구성된 종합 리포트입니다."
        )

    with dl_col3:
        st.download_button(
            label=f"📄 [{target_student.get('name')}] 피드백 (.csv)",
            data=csv_bytes,
            file_name=f"{clean_dept}_{clean_sid}_{clean_sname}_정오표피드백.csv",
            mime="text/csv",
            use_container_width=True,
            help="선택한 학생의 현재 정오표 및 피드백 데이터를 한글 엑셀 호환 CSV 형식으로 다운로드합니다."
        )

    st.markdown('</div>', unsafe_allow_html=True)
