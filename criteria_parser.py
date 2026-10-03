import json
import re
import pandas as pd
from typing import Dict, Any, List, Optional
from upstage_client import UpstageClient

class CriteriaParser:
    """교사가 업로드한 [문제지], [답안지 양식], [정답 및 배점표] 3개 문서를 융합 분석하여 동적 채점 기준표를 생성하는 모듈"""
    
    SYSTEM_PROMPT = """당신은 학교 시험 평가 전문가입니다.
교사가 제공한 [1. 문제지], [2. 답안지 양식], [3. 정답 및 배점기준표] 문서 내용을 종합적으로 교차 검증하여, 실제 학생 답안 채점에 필요한 '통합 채점 기준표'를 만드세요.

[규칙]
1. 문항 번호(item_no) 체계:
   - 답안지 양식과 문제지에 나타난 번호 체계를 종합하여 결정하세요.
   - 대문항에 소문항이 있는 경우 반드시 '1-1', '1-2', '6-1', '6-2'와 같이 하이픈(-) 또는 점(.)으로 모든 세부 평가 단위를 분리하세요.
   - 구글 스프레드시트 결과표의 열 헤더로 바로 사용될 수 있도록 순서대로 정렬하세요.
2. 배점(score):
   - 정답 및 배점기준표에 명시된 배점(숫자, float/int)을 정확히 기록하세요.
3. 문제 유형(type):
   - 답안지의 작성란 형태(객관식 번호, 단답 빈칸, 서술형 박스)를 고려하여 '객관식', '단답형', '서술형' 중 하나로 지정하세요.
4. 정답(correct_answer):
   - 정답지에서 해당 문항의 모범 답안을 추출하여 기록하세요.
5. 채점 기준 및 키워드(rubric):
   - 서술형 부분점수 감점/가점 기준 및 필수 키워드를 요약하여 기록하세요.

반드시 유효한 JSON 형식으로만 응답해야 합니다.
JSON 스키마:
{
  "exam_title": "시험 명칭 (추정)",
  "total_score": 총배점(숫자),
  "items": [
    {
      "item_no": "1-1",
      "score": 3.0,
      "type": "객관식",
      "correct_answer": "4",
      "rubric": "정답 일치 시 3점"
    },
    ...
  ]
}
"""

    def __init__(self, upstage_client: UpstageClient):
        self.client = upstage_client

    def _extract_doc_text(self, file_content: bytes, filename: str) -> str:
        """단일 문서 파일에서 Upstage Document Parse로 텍스트/표 내용을 추출합니다."""
        res = self.client.parse_document(file_content, filename)
        if "content" in res and "html" in res["content"]:
            return res["content"]["html"]
        elif "elements" in res:
            return "\n".join([el.get("text", "") for el in res["elements"]])
        return str(res)

    def parse_three_materials(
        self,
        question_content: Optional[bytes] = None,
        question_name: str = "question.pdf",
        template_content: Optional[bytes] = None,
        template_name: str = "template.pdf",
        answer_key_content: Optional[bytes] = None,
        answer_key_name: str = "answer_key.pdf"
    ) -> Dict[str, Any]:
        """
        교사가 업로드한 3가지 파일(문제지, 답안지 양식, 정답/배점표)을 각각 파싱하여 통합 채점 기준표를 도출합니다.
        """
        combined_context = []
        
        # 1. 문제지 파싱
        if question_content:
            q_text = self._extract_doc_text(question_content, question_name)
            combined_context.append(f"### [1. 문제지 내용 ({question_name})]\n{q_text[:7000]}\n")
            
        # 2. 답안지 양식 파싱
        if template_content:
            t_text = self._extract_doc_text(template_content, template_name)
            combined_context.append(f"### [2. 답안지 양식 내용 ({template_name})]\n{t_text[:7000]}\n")
            
        # 3. 정답 및 배점기준표 파싱
        if answer_key_content:
            a_text = self._extract_doc_text(answer_key_content, answer_key_name)
            combined_context.append(f"### [3. 정답 및 배점기준표 내용 ({answer_key_name})]\n{a_text[:7000]}\n")

        if not combined_context:
            raise ValueError("최소 1개 이상의 시험 문서 파일(문제지, 답안지, 정답지)을 업로드해주세요.")

        full_doc_str = "\n".join(combined_context)

        prompt = f"""다음은 교사가 업로드한 시험 자료들입니다.
제공된 문제지, 답안지 양식, 정답 및 배점기준표를 면밀히 대조 분석하여, 실제 학생 답안 채점에 필요한 모든 세부 문항(소문항 포함)의 번호, 배점, 유형, 정답, 루브릭을 추출하여 JSON으로 작성하세요.

{full_doc_str}
"""
        response_text = self.client.call_solar(
            prompt=prompt,
            system_instruction=self.SYSTEM_PROMPT,
            temperature=0.1,
            response_format_json=True
        )
        
        try:
            criteria_data = json.loads(response_text)
        except Exception:
            clean_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", response_text.strip(), flags=re.MULTILINE)
            criteria_data = json.loads(clean_json)
            
        return criteria_data

    @staticmethod
    def to_dataframe(criteria_data: Dict[str, Any]) -> pd.DataFrame:
        """기준표 딕셔너리를 교사가 확인/수정하기 쉬운 DataFrame으로 변환합니다."""
        items = criteria_data.get("items", [])
        rows = []
        for item in items:
            rows.append({
                "문항번호": str(item.get("item_no", "")),
                "배점": float(item.get("score", 0)),
                "유형": str(item.get("type", "단답형")),
                "정답": str(item.get("correct_answer", "")),
                "채점기준": str(item.get("rubric", ""))
            })
        return pd.DataFrame(rows)

    @staticmethod
    def from_dataframe(df: pd.DataFrame, exam_title: str = "시험") -> Dict[str, Any]:
        """교사가 화면에서 수정한 DataFrame을 채점 엔진에서 사용할 criteria 딕셔너리로 역변환합니다."""
        items = []
        total_score = 0.0
        for _, row in df.iterrows():
            score = float(row.get("배점", 0))
            total_score += score
            items.append({
                "item_no": str(row.get("문항번호", "")).strip(),
                "score": score,
                "type": str(row.get("유형", "단답형")).strip(),
                "correct_answer": str(row.get("정답", "")).strip(),
                "rubric": str(row.get("채점기준", "")).strip()
            })
        return {
            "exam_title": exam_title,
            "total_score": total_score,
            "items": items
        }
