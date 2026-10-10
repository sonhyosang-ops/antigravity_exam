"""
student_grader.py
학생 답안지 OCR 파싱 및 기준표 기반 자동 채점 엔진 (최종판)

성능·정책 개선 사항
-------------------
1. 전처리: 중앙값 필터 → 적응형 이진화 → 기울기 보정 → 답안 영역 크롭 → 업스케일
2. OCR 호출/서술형 채점 API: Exponential Backoff 재시도
3. 추출 프롬프트에 "판독 불확실 항목" 필드 반영 → 1차 인식 결과 원문 그대로 유지
4. 불확실 문항 → 채점은 일단 0점(틀린 것으로) + "확인필요" 태그 + 1차 인식 원문 보존
5. 인간 채점자 확인 단계: review_queue + confirm_answer 인터페이스
6. 객관식·단답형 OCR 변형 정규화(괄호·전각·원숫자·신디게이트 등)
7. 영문 합성어 간단 정리(선택)
8. 긴 OCR 텍스트 청크 분할 후 병합(뒷 문항 누락 방지)
9. 다중 파일 병렬 처리(process_batch)
10. 기울기 보정 방향 검증(deskew_verify_sample) 옵션

사용 예시는 파일 하단 주석 참조.
"""

from __future__ import annotations

import json
import logging
import re
import time
import io
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional
try:
    import cv2  # type: ignore # pyrefly: ignore
except ImportError:
    cv2 = None
import numpy as np
from PIL import Image

# 프로젝트 환경에 맞게 임포트 조정
from upstage_client import UpstageClient


# ------------------------------------------------------------------
# 로깅
# ------------------------------------------------------------------
LOG = logging.getLogger("StudentGraider")
if not LOG.handlers:
    LOG.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    LOG.addHandler(_h)


# ==================================================================
# 학생 답안지 OCR 파싱 및 기준표 기반 자동 채점 엔진 (최종판)
# ==================================================================
class StudentGrader:
    """
    학생 답안지 전처리 → OCR → 정보·답안 추출 → 문항별 채점 파이프라인.

    핵심 정책
    ---------
    - OCR/추출 결과의 '확실하지 않은 답안'은 채점에서 일단 0점(틀린 것으로) 처리하되,
      1차 인식 결과 원문을 그대로 보존하고 "확인필요" 태그를 달아 인간 채점자가
      최종 결정할 수 있도록 사전 단계(review_queue + confirm_answer)를 제공한다.
    - 원문 그대로(as-is) 추출 원칙을 지키며, 불확실 답안을 임의로 교정하지 않는다.
    """

    # ------------------------------------------------------------------
    # 추출 프롬프트
    # ------------------------------------------------------------------
    EXTRACT_PROMPT = """당신은 시험 답안지 OCR 결과를 정밀하게 분석하여 학생 기본 정보와 문항별 작성 답안을 있는 그대로 추출하는 전문 분석가입니다.
주어진 답안지 텍스트에서 다음 정보를 추출하여 반드시 JSON으로 응답하세요.

[추출 규칙 및 중요 지침]
1. 원문 그대로(as-is) 추출:
   - 학생이 실제로 작성한 글씨와 답안 텍스트를 절대로 임의로 교정하거나, 오타를 고치거나, 정답에 맞추어 유추·보정하지 마세요.
   - 학생이 잘못 적은 글자, 오탈자, 불완전한 표기가 있다면 적혀 있는 그대로 정확히 추출해야 공정한 채점이 가능합니다.
2. department: 학생의 학과명 (예: '기계공학과', '전자과', '소프트웨어과' 등. 명시되지 않았다면 '일반' 또는 '미기재')
3. student_id: 학번 (예: '2024001', '10101' 등)
4. name: 학생 성명 (예: '홍길동')
5. answers: 문항 번호를 키로 하고, 학생이 작성/선택한 답안 내용을 값으로 하는 객체.
   기준 문항 목록: {item_list}
   위 기준 문항 목록에 있는 번호와 정확히 일치시켜서 학생의 답안을 매핑하세요. 답을 적지 않은 문항은 빈 문자열("")로 표시하세요.
6. 선지 표기 매핑:
   - "①: A ②: B" 형태이면, 해당 문항 N의 ①=A, ②=B로 매핑해 answers["N-1"]=A, answers["N-2"]=B 형태로 출력하세요.
   - 번호 표기가 섞여 있어도(예: "①문화 ②: 반복 3.③ ...") 가능한 한 문항·선지 번호를 추정해 매핑하되, 불확실한 선지는 [?] 표시 + uncertain 목록 처리하세요.
7. 판독 관련:
   - 판독이 흔들리거나 OCR에서도 잘 안 읽힌 항목은, 해당 문항 번호를 정확히 적은 뒤 uncertain 목록에 아래처럼 넣으세요.
       예: {{"uncertain": ["1-2: 광합성 작ㅇ용", "2: 4"]}}
   - '확실치 않다'는 기준은 OCR 판독의 자신감에 관한 것이며, 학생 답안 내용을 추측·교정하려는 것이 아닙니다. 1차 인식 결과가 그대로 answer에 들어가야 합니다.
   - 빈칸(답안이 없음)은 빈문자열 ""로, 아무것도 없는데 글자가 읽힌 것처럼 보이면 해당 문항을 uncertain에 넣고 답안은 ""로 두세요.
   - 숫자 문항(2, 3, 4, 5 등)에서 숫자가 불명확하면 원문 그대로 + uncertain 표시.
   - 불확실 항목이 없으면 "uncertain": []로 표시하세요.

[JSON 응답 스키마]
{{
  "department": "기계공학과",
  "student_id": "2024001",
  "name": "홍길동",
  "answers": {{
    "1-1": "문제",
    "1-2": "추상화",
    "2": "4"
  }},
  "uncertain": ["1-2: 광합성 작ㅇ용"]
}}
"""

    # ------------------------------------------------------------------
    # 서술형 채점 프롬프트
    # ------------------------------------------------------------------
    ESSAY_GRADE_PROMPT = """당신은 엄격하고 공정한 학교 시험 채점관입니다.
다음 서술형/단답형 문제에 대해 학생의 답안을 기준 정답 및 루브릭과 비교하여 점수를 매기세요.

[문항 정보]
- 문항 번호: {item_no}
- 배점: {max_score}점
- 모범 정답: {correct_answer}
- 채점 기준 및 루브릭: {rubric}

[학생이 실제 작성한 답안]
{student_answer}

[채점 규칙]
- 만점(status='O'): 모범 정답의 핵심 개념과 필수 키워드가 명확히 포함된 경우 ({max_score}점)
- 부분점수(status='△'): 핵심 내용이 일부 누락되었거나 미흡하지만 근접한 경우 (루브릭 기준에 따라 0점 초과 {max_score}점 미만의 점수 부여)
- 오답(status='X'): 오개념이거나 빈칸, 전혀 다른 내용을 쓴 경우 (0점)
- 한/영 및 통용 약어 동등성 인정 규칙: 한국어 개념에 대해 널리 쓰이는 표준 영문 표기 또는 영문 약어(예: 컴퓨터↔Computer, 인공지능↔AI, 운영체제↔OS, 소프트웨어↔SW 등)를 작성한 경우 언어 차이에 불과하므로 동일한 정답/키워드로 완전 인정(만점)합니다.
- 복수 선택형 정답 규칙: 모범 정답에 'A 또는 B', 'A/B'와 같이 선택적 정답이 명시된 경우, 둘 중 하나를 올바르게 작성하면 만점(status='O')입니다. 단, 'A 또는 B'처럼 선택지 전체를 그대로 베껴 쓴 것은 오답(status='X', 0점)으로 엄격히 처리하세요.

반드시 다음 JSON 형식으로만 응답하세요:
{{
  "score": 득점(숫자),
  "status": "O" 또는 "△" 또는 "X",
  "feedback": "채점 사유 요약 (1문장)"
}}
"""

    # ------------------------------------------------------------------
    # 초기화
    # ------------------------------------------------------------------
    def __init__(
        self,
        client: UpstageClient,
        *,
        max_retries: int = 3,
        parallel_workers: int = 3,
        llm_workers: int = 4,
        short_answer_llm_rejudge: bool = True,
        crop_answer_ratio: float = 0.0,
        target_min_px: int = 2000,
        deskew_verify_sample: Optional[str] = None,
        enable_english_cleanup: bool = True,
    ):
        """
        Parameters
        ----------
        client: UpstageClient 인스턴스 (parse_document, call_solar 제공)
        max_retries: API 호출 실패 시 재시도 횟수
        parallel_workers: 여러 답안지 병렬 처리 스레드 수
        llm_workers: 한 학생 내 문항별 LLM 채점(서술형/단답형 재판단) 동시 호출 수
        short_answer_llm_rejudge: 단답형 완전 불일치 시 자모/철자 혼동 2차 판별 사용 여부
        crop_answer_ratio: 전처리 시 답변 영역 확보를 위해 상단을 제거할 비율(0~1).
                            주의: 상단에 학과/학번/성명이 있는 답안지는 0으로 두어야 함(기본값 0)
        target_min_px: 전처리 후 긴 축 최소 픽셀 수(OCR 해상도 여유)
        deskew_verify_sample: 기울기 보정 방향이 맞는지 확인할 샘플 이미지 경로.
                              제공 시 해당 이미지로 한 번 테스트해 회전 방향을 보정 시도.
        enable_english_cleanup: 영문 합성어 간단 정리 기능 사용 여부
        """
        self.client = client
        self.max_retries = max_retries
        self.parallel_workers = parallel_workers
        self.llm_workers = max(1, llm_workers)
        self.short_answer_llm_rejudge = short_answer_llm_rejudge
        self.crop_answer_ratio = crop_answer_ratio
        self.target_min_px = target_min_px
        self.deskew_verify_sample = deskew_verify_sample
        self.enable_english_cleanup = enable_english_cleanup
        self._deskew_flip = self._check_deskew_direction() if deskew_verify_sample else False

    # ------------------------------------------------------------------
    # API 호출 재시도 (Exponential Backoff)
    # ------------------------------------------------------------------
    def _retry(self, fn: Callable, *args, max_retries: Optional[int] = None,
               base_delay: float = 1.0, **kwargs):
        """임의의 호출(API)을 재시도."""
        max_retries = max_retries if max_retries is not None else self.max_retries
        for attempt in range(1, max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if attempt >= max_retries:
                    LOG.error(f"최대 재시도 횟수 초과 (시도 {attempt}/{max_retries}): {e}")
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                LOG.warning(
                    f"호출 실패(시도 {attempt}/{max_retries}): {e}  "
                    f"— {delay:.1f}초 후 재시도"
                )
                time.sleep(delay)

    # ------------------------------------------------------------------
    # 전처리 — 손글씨 답안지 최적화
    # ------------------------------------------------------------------
    def enhance_handwriting(self, file_content: bytes, filename: str) -> bytes:
        """
        학생 손글씨 답안지 이미지 전처리.
        - RGBA/LA 투명 배경 → 흰색 배경 합성
        - 중앙값 필터(노이즈)
        - 적응형 이진화(조명 편차 보정)
        - 기울기 보정(회전 방향 검증 후 적용)
        - 답안 영역 크롭(설정 비율)
        - 저해상도 업스케일
        """
        ext = filename.lower().split(".")[-1]
        if ext not in ("jpg", "jpeg", "png", "bmp", "tiff", "webp"):
            return file_content  # PDF 등 그대로 통과
        if cv2 is None:
            return file_content  # OpenCV 미설치 시 전처리 생략

        try:
            img = Image.open(io.BytesIO(file_content))

            # 투명 배경 → 흰 배경 합성
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # PIL → 그레이스케일 numpy
            arr = np.array(img.convert("L"))

            # 1) 중앙값 필터 (노이즈 완화)
            arr = cv2.medianBlur(arr, 3)

            # 2) 적응형 이진화 (연필·볼펜·조명 편차 대응)
            #    ※ OpenCV는 키워드 인자(block_size=)를 받지 않아 기존 코드가 항상 실패했음 → 위치 인자로 수정
            bw = cv2.adaptiveThreshold(
                arr, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                31,  # blockSize
                15,  # C
            )

            # 3) 기울기 보정
            bw = self._deskew(bw)

            # 4) 답안 영역 크롭 (설정 비율 상단 제거 — 답안지 배치에 맞게 조정 필요)
            h = bw.shape[0]
            top = int(h * self.crop_answer_ratio)
            if top > 0:
                bw = bw[top:, :]

            # 5) 필요 시 업스케일 (OCR 해상도 여유 확보)
            h, w = bw.shape
            if max(w, h) < self.target_min_px:
                scale = self.target_min_px / max(w, h)
                new_size = (int(w * scale), int(h * scale))
                bw = cv2.resize(bw, new_size, interpolation=cv2.INTER_LANCZOS4)

            out = Image.fromarray(bw)
            out_buf = io.BytesIO()
            out.save(out_buf, format="JPEG", quality=95)
            return out_buf.getvalue()

        except Exception as e:
            LOG.warning(f"전처리 실패({filename}): {e} — 원본을 그대로 반환")
            return file_content

    def _deskew(self, bw: np.ndarray) -> np.ndarray:
        """minAreaRect 기반 기울기 보정. 회전 방향이 틀리면 _deskew_flip로 보정."""
        coords = cv2.findNonZero((bw < 128).astype(np.uint8))
        if coords is None or len(coords) < 50:
            return bw  # 텍스트 영역 부족 → 보정 생략

        angle = cv2.minAreaRect(coords)[-1]
        # OpenCV 버전별 각도 범위(-90~0 / 0~90) 차이를 [-45, 45]로 정규화
        while angle > 45:
            angle -= 90
        while angle < -45:
            angle += 90
        angle = -angle

        # 미세하거나(불필요) 과도한(오감지 가능성) 각도는 회전하지 않음
        if abs(angle) < 0.3 or abs(angle) > 15:
            return bw

        if self._deskew_flip:
            angle = -angle

        h, w = bw.shape
        M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
        return cv2.warpAffine(
            bw, M, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )

    def _check_deskew_direction(self) -> bool:
        """
        제공 샘플 이미지로 회전 방향이 맞는지 확인.
        방향이 반대면 _deskew_flip = True로 설정.
        (구현 환경에 따라 간단한 수동 검증용 — 필요시 보정)
        """
        if not self.deskew_verify_sample:
            return False
        try:
            verify_img = Image.open(self.deskew_verify_sample).convert("L")
            arr = np.array(verify_img)
            arr = cv2.medianBlur(arr, 3)
            bw = cv2.adaptiveThreshold(
                arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 15,
            )
            coords = cv2.findNonZero(bw < 128)
            if coords is None or len(coords) < 50:
                return False
            angle = cv2.minAreaRect(coords)[-1]
            # 예시 규칙: 각도가 -30~-60 범위면 '세로 편향'으로 보고 flip 필요 판단
            # (실제 답안지 샘플로 테스트 후 임계값을 조정)
            return -60 <= angle <= -30
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 정규화 유틸 — 객관식·단답형 OCR 변형 대응
    # ------------------------------------------------------------------
    _CIRCLENUM_MAP = {
        "①": "1", "②": "2", "③": "3", "④": "4", "⑤": "5",
        "⑥": "6", "⑦": "7", "⑧": "8", "⑨": "9", "⑩": "10",
    }

    @classmethod
    def _normalize_text(cls, text: str) -> str:
        """공백·기호·전각문자·원숫자·따옴표 등을 완벽히 정리하고 유니코드를 NFC로 정규화."""
        if not text:
            return ""
        # 1. macOS 자모 분리(NFD) -> 완성형(NFC) 정규화 (필수!)
        t = unicodedata.normalize("NFC", str(text))
        t = t.strip().lower()

        # 2. 다양한 공백(NBSP, 탭, 줄바꿈, zero-width space 등) 제거
        t = re.sub(r"[\s\u00a0\u200b\ufeff\r\n\t]+", "", t)

        # 3. 따옴표(큰따옴표, 작은따옴표, 스마트따옴표 등) 및 문장부호 제거
        t = re.sub(r"[\'\"‘’“”`\'\"]", "", t)
        t = re.sub(r"[\.\，\(\)\[\]\{\}「」『』〈〉《》:;·、·\\/\|~!?@#$%^&*_+=]", "", t)

        # 4. 전각 영숫자 → 반각
        t = t.translate(str.maketrans(
            "０１２３４５６７８９ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
            "0123456789abcdefghijklmnopqrstuvwxyz",
        ))

        # 5. 원숫자 치환
        for k, v in cls._CIRCLENUM_MAP.items():
            t = t.replace(k, v)

        return t

    @classmethod
    def normalize_option_label(cls, text: str) -> str:
        """객관식 보기 답안의 OCR 변형을 고려한 정규화."""
        return cls._normalize_text(text)

    @classmethod
    def normalize_short_answer(cls, text: str) -> str:
        """단답형 비교를 위한 정규화."""
        return cls._normalize_text(text)

    # ------------------------------------------------------------------
    # 한-영 및 통용 약어 표준 동의어 매핑 (채점 규칙 일반화)
    # ------------------------------------------------------------------
    _COMMON_EQUIVALENTS: Dict[str, set[str]] = {
        "컴퓨터": {"computer", "컴퓨터"},
        "인공지능": {"ai", "artificialintelligence", "인공지능"},
        "소프트웨어": {"software", "sw", "소프트웨어"},
        "하드웨어": {"hardware", "hw", "하드웨어"},
        "데이터베이스": {"database", "db", "데이터베이스"},
        "운영체제": {"os", "operatingsystem", "운영체제"},
        "중앙처리장치": {"cpu", "중앙처리장치"},
        "네트워크": {"network", "네트워크"},
        "인터넷": {"internet", "인터넷"},
        "알고리즘": {"algorithm", "알고리즘"},
        "프로그램": {"program", "programme", "프로그램"},
        "프로그래밍": {"programming", "프로그래밍"},
        "파이썬": {"python", "파이썬"},
        "자바": {"java", "자바"},
        "메모리": {"memory", "주기억장치", "메모리"},
        "서버": {"server", "서버"},
        "클라이언트": {"client", "클라이언트"},
        "추상화": {"abstraction", "추상화"},
        "자동화": {"automation", "자동화"},
    }

    @classmethod
    def are_equivalent_terms(cls, term1: str, term2: str) -> bool:
        """
        두 단어가 한-영 표기 차이, 약어, 또는 표준 동의어인지 검사.
        예: '컴퓨터' == 'computer', '인공지능' == 'ai'
        """
        c1 = cls.normalize_short_answer(term1)
        c2 = cls.normalize_short_answer(term2)
        if not c1 or not c2:
            return False
        if c1 == c2:
            return True

        for key, syn_set in cls._COMMON_EQUIVALENTS.items():
            norm_syns = {cls.normalize_short_answer(s) for s in syn_set}
            norm_syns.add(cls.normalize_short_answer(key))
            if c1 in norm_syns and c2 in norm_syns:
                return True

        return False

    @classmethod
    def check_or_alternatives(cls, raw_ans: str, correct_ans: str) -> Optional[tuple[bool, str]]:
        """
        정답에 '또는'(또는 '혹은', '/', '|')이 포함된 경우의 채점 규칙:
        - 앞말만 쓴 경우 -> 정답 (O)
        - 뒷말만 쓴 경우 -> 정답 (O)
        - '또는'을 포함하거나 앞뒤 말을 모두 포함한 경우 -> 오답 (X)
        '또는'류가 없는 일반 정답이면 None 반환.
        반환값: (is_correct, feedback_message) 또는 None
        """
        text_corr = unicodedata.normalize("NFC", str(correct_ans)).strip()
        has_or = any(sep in text_corr for sep in ("또는", "혹은", "/", "|"))
        if not has_or:
            return None

        delimiters = r'(?:\s+또는\s+|\s+혹은\s+|\s*/\s*|\s*\|\s*)'
        parts = [p.strip() for p in re.split(delimiters, text_corr) if p.strip()]
        if len(parts) < 2:
            return None

        clean_raw = cls.normalize_short_answer(raw_ans)
        clean_parts = [cls.normalize_short_answer(p) for p in parts]

        raw_norm = unicodedata.normalize("NFC", str(raw_ans)).strip()

        # 1. 학생 답안에 '또는', '혹은', '/', '|'이 직접 포함되어 있는 경우 -> 오답
        if any(sep in raw_norm for sep in ("또는", "혹은", "/", "|")):
            return (False, f"오답: '또는' 등의 선택 기호를 포함하여 작성한 답안은 인정되지 않습니다 (기준: '{text_corr}', 학생: '{raw_ans}')")

        # 2. 앞뒤 정답 후보 단어가 모두 포함되어 있는 경우 (예: '추상화, 자동화', '추상화 자동화') -> 오답
        contains_all = all(cp in clean_raw for cp in clean_parts if cp)
        if contains_all:
            return (False, f"오답: 정답 후보('{parts[0]}', '{parts[1]}')를 모두 작성하여 불인정됩니다. 둘 중 하나만 선택해야 합니다 (학생: '{raw_ans}')")

        # 3. 앞말 또는 뒷말 중 정확히 하나와 일치하는 경우 (한-영/약어 동등성 포함) -> 정답 (O)
        matched = []
        for orig_p, cp in zip(parts, clean_parts):
            if clean_raw == cp or cls.are_equivalent_terms(raw_ans, orig_p):
                matched.append(orig_p)

        if len(matched) == 1:
            return (True, f"정답 일치: 기준 정답('{text_corr}') 중 선택 답안 '{matched[0]}' 작성 일치")
        elif len(matched) > 1:
            return (False, f"오답: 복수 정답 후보 중복 작성 불인정 (기준: '{text_corr}', 학생: '{raw_ans}')")

        return (False, f"오답: 기준 정답('{text_corr}')의 정답 후보와 불일치 (학생 답안: '{raw_ans}')")

    @classmethod
    def extract_alternative_answers(cls, correct_ans: str) -> List[str]:
        """
        '추상화 또는 자동화', 'A / B', 'A 혹은 B' 등 복수 인정 정답을 분리.
        단, 학생이 '추상화 또는 자동화'라고 통째로 쓴 것은 오답이어야 하므로 후보 목록을 반환.
        """
        text = str(correct_ans).strip()
        if not text:
            return []
        delimiters = r'(?:\s+또는\s+|\s+혹은\s+|\s*/\s*|\s*\|\s*)'
        parts = [p.strip() for p in re.split(delimiters, text) if p.strip()]
        if len(parts) > 1:
            return parts
        return [text]

    # ------------------------------------------------------------------
    # 영문 합성어 간단 정리 (선택)
    # ------------------------------------------------------------------
    _ENGLISH_COMPOUNDS = {
        "hard ware": "Hardware",
        "hw": "Hardware",
        "soft ware": "Software",
        "sw": "Software",
        "w ave": "Wave",
        "wave": "Wave",
        "programing": "programming",
        "programingg": "programming",
    }

    @classmethod
    def clean_english(cls, text: str) -> str:
        """영문 합성어/철자 간단 정리(원문은 유지, 교정 후보만 별도 관리 권장)."""
        if not cls.enable_english_cleanup:
            return text
        t = text.strip()
        low = t.lower()
        if low in cls._ENGLISH_COMPOUNDS:
            return cls._ENGLISH_COMPOUNDS[low]
        # 일반적인 띄어쓰기 합성어 재결합 시도 (간략)
        joined = re.sub(r"\s+", "", low)
        if joined in cls._ENGLISH_COMPOUNDS:
            return cls._ENGLISH_COMPOUNDS[joined]
        return t  # 원문은 유지

    # ------------------------------------------------------------------
    # 텍스트 청크 분할 (긴 OCR 결과 대응)
    # ------------------------------------------------------------------
    @staticmethod
    def _chunk_text(text: str, max_chunk: int = 11000, overlap: int = 1500) -> List[str]:
        """OCR 텍스트가 길면 겹침 있는 청크로 분할."""
        if len(text) <= max_chunk:
            return [text]
        chunks, start = [], 0
        while start < len(text):
            end = min(start + max_chunk, len(text))
            chunks.append(text[start:end])
            if end >= len(text):
                break  # 마지막 청크 — 기존 코드는 여기서 무한 루프에 빠졌음
            start = end - overlap
        return chunks

    # ------------------------------------------------------------------
    # JSON 파싱 유틸
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json_response(response_text: str) -> dict:
        """call_solar 응답을 JSON으로 파싱 (마크다운 코드블록 제거 포함)."""
        s = response_text.strip()
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
        return json.loads(s)

    # ------------------------------------------------------------------
    # 문서 텍스트 추출 유틸
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_doc_text(doc_result: dict) -> str:
        content = doc_result.get("content") or {}
        if content.get("markdown"):
            return content["markdown"]
        if content.get("html"):
            return content["html"]
        if "elements" in doc_result:
            return "\n".join(el.get("text", "") for el in doc_result["elements"])
        return str(doc_result)

    # ------------------------------------------------------------------
    # LLM 채점 단일 호출 (병렬 실행용)
    # ------------------------------------------------------------------
    def _llm_judge(
        self,
        prompt: str,
        system_instruction: str,
        max_score: float,
        default_feedback: str,
        error_feedback: Optional[str],
        label: str,
    ) -> Dict[str, Any]:
        """문항 1개를 LLM으로 채점. error_feedback이 None이면 '자동 채점 오류: ...' 사용."""
        try:
            res = self._retry(
                self.client.call_solar,
                prompt=prompt,
                system_instruction=system_instruction,
                temperature=0.0,
                response_format_json=True,
            )
            res_json = self._parse_json_response(res)
            score = max(0.0, min(max_score, float(res_json.get("score", 0.0))))
            status = str(res_json.get("status", "X")).upper()
            feedback = str(res_json.get("feedback", default_feedback))
        except Exception as e:
            LOG.warning(f"{label} 오류: {e}")
            score, status = 0.0, "X"
            feedback = error_feedback if error_feedback is not None else f"자동 채점 오류: {e}"
        return {"score": score, "status": status, "feedback": feedback, "needs_review": False}

    # ------------------------------------------------------------------
    # 채점 ① — OCR 파싱 → 학생 정보·답안 추출
    # ------------------------------------------------------------------
    def parse_student_sheet(
        self,
        file_content: bytes,
        filename: str,
        criteria_data: Dict[str, Any],
        default_department: str = "",
    ) -> Dict[str, Any]:
        """학생 답안지 1부를 전처리 → OCR → 정보/답안 추출. 불확실 항목도 함께 반환."""

        # 1) 전처리
        optimized_content = self.enhance_handwriting(file_content, filename)

        # 2) Document Parse (OCR)
        doc_result = self._retry(
            self.client.parse_document, optimized_content, filename
        )
        doc_text = self._extract_doc_text(doc_result)

        # 3) 기준 문항 번호 목록
        item_keys = [item["item_no"] for item in criteria_data.get("items", [])]

        # 4) 추출 프롬프트
        system_inst = self.EXTRACT_PROMPT.format(
            item_list=", ".join(item_keys)
        )

        # 5) 텍스트가 길면 청크 분할 후 LLM 호출 → 병합
        chunks = self._chunk_text(doc_text, max_chunk=11000, overlap=1500)
        merged: Dict[str, Any] = {
            "department": "",
            "student_id": "",
            "name": "",
            "answers": {},
            "uncertain": [],
        }

        for chunk in chunks:
            prompt = f"""[답안지 파싱 내용]
{chunk}
"""
            try:
                response = self._retry(
                    self.client.call_solar,
                    prompt=prompt,
                    system_instruction=system_inst,
                    temperature=0.0,
                    response_format_json=True,
                )
                res = self._parse_json_response(response)
            except Exception as e:
                LOG.warning(f"답안지 추출 중 파싱 오류({filename}): {e}")
                res = {}

            # 병합: 비어있지 않은 첫 값을 유지 (중복 문항 대응)
            for key in ("department", "student_id", "name"):
                if not merged.get(key):
                    merged[key] = res.get(key, "")
            for q, ans in res.get("answers", {}).items():
                if not merged["answers"].get(q):
                    merged["answers"][q] = ans
            for u in res.get("uncertain", []):
                if u not in merged["uncertain"]:
                    merged["uncertain"].append(u)

        # 6) 학과명 보정
        dept = merged.get("department", "").strip()
        if not dept or dept in ("미기재", "일반", "알수없음"):
            merged["department"] = default_department or "일반"
        else:
            merged["department"] = dept

        return merged

    # ------------------------------------------------------------------
    # uncertain 목록 → 문항번호 → 1차 인식 원문 매핑
    # ------------------------------------------------------------------
    @staticmethod
    def _build_uncertain_item_set(extract_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        extract 결과의 uncertain 목록을 파싱해
        {item_no: 1차 인식 결과 원문} 형태로 반환 + 파싱 실패 항목 분리.
        예: ["1-2: 광합성 작ㅇ용", "2: 4"]  →  {"1-2": "광합성 작ㅇ용", "2": "4"}
        """
        raw_uncertain = extract_result.get("uncertain", [])
        if isinstance(raw_uncertain, dict):
            uncertain_list = [f"{k}: {v}" for k, v in raw_uncertain.items()]
        elif isinstance(raw_uncertain, list):
            uncertain_list = raw_uncertain
        else:
            uncertain_list = [str(raw_uncertain)] if raw_uncertain else []

        mapping: Dict[str, Any] = {}
        unparsed: List[str] = []
        for entry in uncertain_list:
            if not isinstance(entry, str):
                entry = str(entry)
            entry = entry.strip()
            if not entry:
                continue
            m = re.match(r"^([0-9]+(?:-[0-9]+)?)\s*[:：]?\s*(.+)$", entry)
            if m:
                item_no, text = m.group(1), m.group(2).strip()
                mapping[item_no] = text
            else:
                unparsed.append(entry)
        mapping["_unparsed_uncertain"] = unparsed
        return mapping

    # ------------------------------------------------------------------
    # 채점 ② — 문항별 채점 (객관식 / 단답형 / 서술형 + 확인필요 정책)
    # ------------------------------------------------------------------
    def grade_student(
        self,
        student_info: Dict[str, Any],
        criteria_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        추출된 학생 답안을 기준표와 대조해 개별 문항 채점 및 총점 계산.

        정책:
        - 불확실한 문항(추출 결과의 uncertain에 포함된 문항) → 채점 0점(틀린 것으로) +
          "확인필요" 태그 + 1차 인식 결과 원문 보존 + review_queue 등록.
        - 원문 그대로 원칙에 따라 불확실 답안을 임의로 교정하지 않는다.
        """

        criteria_items = {
            item["item_no"]: item for item in criteria_data.get("items", [])
        }
        student_answers = student_info.get("answers", {})

        # uncertain 목록 → 문항번호 → 1차 인식 원문 매핑
        uncertain_data = self._build_uncertain_item_set(student_info)
        unparsed_uncertain = uncertain_data.pop("_unparsed_uncertain", [])
        uncertain_items = set(uncertain_data.keys())

        graded_results: Dict[str, Any] = {}
        total_student_score = 0.0
        review_queue: List[Dict[str, Any]] = []  # 인간 확인용 큐
        llm_jobs: List[tuple] = []  # (item_no, raw_ans, _llm_judge kwargs) — 루프 후 병렬 실행

        for item_no, crit in criteria_items.items():
            max_score = float(crit.get("score", 0))
            correct_ans = str(crit.get("correct_answer", "")).strip()
            item_type = crit.get("type", "단답형")
            rubric = crit.get("rubric", "")

            raw_ans = str(student_answers.get(item_no, "")).strip()

            # ------------------------------------------------------
            # 불확실(확인필요) 답안 → 채점 0점 + 확인필요 태그 + 원문 보존
            # ------------------------------------------------------
            if item_no in uncertain_items:
                preliminary_answer = uncertain_data[item_no]

                graded_results[item_no] = {
                    "answer": preliminary_answer,          # 1차 인식 결과 원문 (교정 없음)
                    "score": 0.0,                          # 일단 틀린 것으로 처리
                    "status": "?",                         # 확인필요 상태
                    "feedback": "인식 불확실 — 확인필요",
                    "needs_review": True,
                    "preliminary_answer": preliminary_answer,
                }
                review_queue.append({
                    "item_no": item_no,
                    "preliminary_answer": preliminary_answer,
                    "max_score": max_score,
                    "correct_answer": correct_ans,
                    "rubric": rubric,
                    "item_type": item_type,
                })
                continue

            # ------------------------------------------------------
            # 빈 답안
            # ------------------------------------------------------
            if not raw_ans:
                graded_results[item_no] = {
                    "answer": "",
                    "score": 0.0,
                    "status": "X",
                    "feedback": "답안 미작성",
                }
                continue

            # ------------------------------------------------------
            # 객관식: 정규화된 보기 라벨 비교
            # ------------------------------------------------------
            if item_type == "객관식":
                clean_raw = self.normalize_option_label(raw_ans)
                clean_corr = self.normalize_option_label(correct_ans)
                if clean_raw == clean_corr:
                    score = max_score
                    status = "O"
                    feedback = f"정답 일치 ({correct_ans}번)"
                else:
                    score = 0.0
                    status = "X"
                    feedback = f"오답: 기준 정답 {correct_ans}번 (학생 선택: {raw_ans}번)"
                graded_results[item_no] = {
                    "answer": raw_ans,
                    "score": score,
                    "status": status,
                    "feedback": feedback,
                    "needs_review": False,
                }
                total_student_score += score
                continue

            # ------------------------------------------------------
            # 단답형: 완전 일치 검사 (정규화) + 선택: 자모/철자 혼동 2차 판별
            # ------------------------------------------------------
            if item_type == "단답형":
                # '또는'/'혹은' 복수 선택형 정답 우선 검증 (앞/뒤 단독 작성만 정답 인정, 둘 다 포함이나 '또는' 포함은 오답)
                or_check = self.check_or_alternatives(raw_ans, correct_ans)
                if or_check is not None:
                    is_correct, feedback_msg = or_check
                    score = max_score if is_correct else 0.0
                    status = "O" if is_correct else "X"
                    graded_results[item_no] = {
                        "answer": raw_ans,
                        "score": score,
                        "status": status,
                        "feedback": feedback_msg,
                        "needs_review": False,
                    }
                    total_student_score += score
                    continue

                clean_raw = self.normalize_short_answer(raw_ans)
                candidates = self.extract_alternative_answers(correct_ans)

                if len(candidates) > 1:
                    # 복수 선택형 정답 (예: '추상화 또는 자동화')
                    clean_full = self.normalize_short_answer(correct_ans)
                    if clean_raw == clean_full:
                        graded_results[item_no] = {
                            "answer": raw_ans,
                            "score": 0.0,
                            "status": "X",
                            "feedback": f"오답: 복수 정답 후보 중 하나만 작성해야 합니다 ('{correct_ans}' 병기 불인정)",
                            "needs_review": False,
                        }
                        continue

                    # 후보 중 정확히 하나와 일치하는지 검사 (한-영 및 약어 동의어 포함)
                    matched_cand = None
                    matched_count = 0
                    for cand in candidates:
                        if clean_raw == self.normalize_short_answer(cand) or self.are_equivalent_terms(raw_ans, cand):
                            matched_cand = cand
                            matched_count += 1

                    if matched_count == 1:
                        graded_results[item_no] = {
                            "answer": raw_ans,
                            "score": max_score,
                            "status": "O",
                            "feedback": f"정답 일치: 기준 정답 '{correct_ans}' 중 선택 답안 '{matched_cand}' 일치 (한/영·약어 인정)",
                            "needs_review": False,
                        }
                        total_student_score += max_score
                        continue
                    elif matched_count > 1:
                        graded_results[item_no] = {
                            "answer": raw_ans,
                            "score": 0.0,
                            "status": "X",
                            "feedback": f"오답: 복수 정답 후보 중복 작성 불인정 (기준: '{correct_ans}', 작성: '{raw_ans}')",
                            "needs_review": False,
                        }
                        continue

                    # 후보 중 일치하는 것이 없을 때: 자모/철자 2차 판별 및 상세 피드백 생성
                    if self.short_answer_llm_rejudge:
                        rejudge_prompt = f"""당신은 학교 시험 채점관입니다.
학생 답안과 모범 정답 후보들을 대조하여 채점 결과와 상세 피드백을 JSON으로 작성하세요.

[문항 정보]
- 문항 번호: {item_no}
- 배점: {max_score}점
- 모범 정답 후보: {', '.join(candidates)}
- 학생 답안: {raw_ans}

[채점 및 피드백 규칙]
1. 한/영 표기 및 통용 약어 인정 규칙 (최우선 적용):
   - 학생이 모범 정답 후보 중 하나의 통용 영문 단어나 약어(예: 컴퓨터↔Computer, 인공지능↔AI, 운영체제↔OS, 소프트웨어↔SW 등)를 작성한 경우 언어 차이에 불과하므로 반드시 정답(status='O', {max_score}점)을 부여하세요.
2. 학생이 정답 후보 중 하나를 골라 썼으나 단순 오타나 자모 누락인 경우 정답 또는 부분점수를 부여하세요.
3. 후보 전체를 베껴 썼거나 전혀 다른 오답인 경우 0점을 부여하고, "정답 후보와 학생 답안이 왜 불일치하는지" 1문장의 구체적인 이유를 피드백으로 작성하세요.

반드시 다음 JSON 형식으로 응답하세요:
{{
  "score": 점수(숫자),
  "status": "O" 또는 "△" 또는 "X",
  "feedback": "상세 채점 피드백 (1문장)"
}}
"""
                        graded_results[item_no] = None  # 자리 확보(문항 순서 유지)
                        llm_jobs.append((item_no, raw_ans, dict(
                            prompt=rejudge_prompt,
                            system_instruction="",
                            max_score=max_score,
                            default_feedback=f"오답: 정답 후보({', '.join(candidates)})와 불일치",
                            error_feedback=f"오답: 정답 후보({', '.join(candidates)})와 불일치 (학생 답안: '{raw_ans}')",
                            label=f"단답형 재판단({item_no})",
                        )))
                        continue

                    # 재판단 끄면 상세 오답 피드백
                    graded_results[item_no] = {
                        "answer": raw_ans,
                        "score": 0.0,
                        "status": "X",
                        "feedback": f"오답: 정답 후보({', '.join(candidates)})와 불일치 (학생 답안: '{raw_ans}')",
                        "needs_review": False,
                    }
                    continue

                else:
                    # 단일 정답
                    clean_corr = self.normalize_short_answer(correct_ans)
                    if clean_raw == clean_corr or self.are_equivalent_terms(raw_ans, correct_ans):
                        graded_results[item_no] = {
                            "answer": raw_ans,
                            "score": max_score,
                            "status": "O",
                            "feedback": f"정답 일치 (기준: '{correct_ans}')",
                            "needs_review": False,
                        }
                        total_student_score += max_score
                        continue

                    # 완전 불일치 → 자모/철자 혼동 및 오답 사유 2차 판정
                    if self.short_answer_llm_rejudge:
                        rejudge_prompt = f"""당신은 학교 시험 채점관입니다.
학생 답안과 모범 정답을 비교하여 채점 결과와 상세 피드백을 JSON으로 작성하세요.

[문항 정보]
- 문항 번호: {item_no}
- 배점: {max_score}점
- 모범 정답: {correct_ans}
- 학생 답안: {raw_ans}

[채점 및 피드백 규칙]
1. 한/영 표기 및 통용 약어 인정 규칙 (최우선 적용):
   - 모범 정답이 한글이고 학생이 통용되는 영문 단어나 약어(예: 컴퓨터↔Computer, 인공지능↔AI, 운영체제↔OS, 소프트웨어↔SW 등)를 작성한 경우, 또는 반대로 영문 정답을 한글로 쓴 경우 언어 및 약어 표기 차이일 뿐 본질적으로 동일한 개념이므로 반드시 만점(status='O', {max_score}점)을 부여하세요.
2. 단순 자모 누락이나 맞춤법 오타이지만 핵심 개념이 모범 정답과 본질적으로 같으면 정답(status='O', {max_score}점) 또는 부분점수(status='△')를 부여하고 사유를 설명하세요.
3. 개념이 상이하거나 오답인 경우 0점(status='X')을 부여하고, "기준 정답 '{correct_ans}'과(와) 학생 답안 '{raw_ans}'이(가) 왜 다른지" 명확하고 구체적인 피드백을 1문장으로 작성하세요.

반드시 다음 JSON 형식으로 응답하세요:
{{
  "score": 점수(숫자),
  "status": "O" 또는 "△" 또는 "X",
  "feedback": "상세 채점 피드백 (1문장)"
}}
"""
                        graded_results[item_no] = None  # 자리 확보(문항 순서 유지)
                        llm_jobs.append((item_no, raw_ans, dict(
                            prompt=rejudge_prompt,
                            system_instruction="",
                            max_score=max_score,
                            default_feedback=f"오답: 기준 정답 '{correct_ans}'과(와) 불일치",
                            error_feedback=f"오답: 기준 정답 '{correct_ans}'과(와) 불일치 (학생 답안: '{raw_ans}')",
                            label=f"단답형 재판단({item_no})",
                        )))
                        continue

                    graded_results[item_no] = {
                        "answer": raw_ans,
                        "score": 0.0,
                        "status": "X",
                        "feedback": f"오답: 기준 정답 '{correct_ans}'과(와) 불일치 (학생 답안: '{raw_ans}')",
                        "needs_review": False,
                    }
                    continue

            # ------------------------------------------------------
            # 서술형: 루브릭 대조 채점 (LLM)
            # ------------------------------------------------------
            grade_prompt = self.ESSAY_GRADE_PROMPT.format(
                item_no=item_no,
                max_score=max_score,
                correct_answer=correct_ans,
                rubric=rubric,
                student_answer=raw_ans,
            )

            graded_results[item_no] = None  # 자리 확보(문항 순서 유지)
            llm_jobs.append((item_no, raw_ans, dict(
                prompt=grade_prompt,
                system_instruction=(
                    "당신은 공정한 채점관입니다. 학생 답안의 원문을 엄정하게 "
                    "평가하여 JSON 형식으로 점수와 정오 상태를 산출하세요."
                ),
                max_score=max_score,
                default_feedback="",
                error_feedback=None,
                label=f"서술형 채점({item_no})",
            )))

        # ------------------------------------------------------
        # LLM이 필요한 문항(서술형 + 단답형 재판단)을 동시에 채점
        # (기존: 문항마다 순차 호출 → 문항 수 × 응답시간만큼 대기)
        # ------------------------------------------------------
        if llm_jobs:
            workers = min(self.llm_workers, len(llm_jobs))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._llm_judge, **kwargs): (item_no, raw_ans)
                    for item_no, raw_ans, kwargs in llm_jobs
                }
                for fut in as_completed(futures):
                    item_no, raw_ans = futures[fut]
                    result = fut.result()
                    result["answer"] = raw_ans
                    graded_results[item_no] = result

        total_student_score = sum(
            float(v.get("score", 0.0)) for v in graded_results.values() if v
        )

        return {
            "department": student_info.get("department", "일반"),
            "student_id": student_info.get("student_id", ""),
            "name": student_info.get("name", ""),
            "results": graded_results,
            "total_score": round(total_student_score, 2),
            "review_queue": review_queue,           # 인간 채점자 확인 대상
            "unparsed_uncertain": unparsed_uncertain,  # 파싱 실패한 불확실 항목
            "uncertain": [str(u) for u in student_info.get("uncertain", [])],
        }

    # ------------------------------------------------------------------
    # 인간 채점자 확인 단계 — 인터페이스
    # ------------------------------------------------------------------
    def get_review_queue(self, grade_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """확인필요한 문항 목록 반환."""
        return grade_result.get("review_queue", [])

    def confirm_answer(
        self,
        grade_result: Dict[str, Any],
        item_no: str,
        final_score: float,
        final_status: str,          # "O" / "△" / "X"
        final_feedback: str = "",
    ) -> Dict[str, Any]:
        """
        인간 채점자가 확인필요 답안을 최종 결정한 결과를 grade_result에 반영.
        결과 객체를 직접 갱신하는 방식(호출부에서 저장/재사용).
        """
        results = grade_result.get("results", {})
        entry = results.get(item_no, {})
        entry["score"] = float(final_score)
        entry["status"] = str(final_status).upper()
        entry["feedback"] = final_feedback or entry.get("feedback", "")
        entry["needs_review"] = False
        entry["confirmed_by_human"] = True
        results[item_no] = entry

        # 총점 재계산
        total = 0.0
        for v in results.values():
            total += float(v.get("score", 0))
        grade_result["total_score"] = round(total, 2)
        return grade_result

    # ------------------------------------------------------------------
    # 검토용 대시보드/요약 출력 (선택)
    # ------------------------------------------------------------------
    def print_review_queue(self, grade_result: Dict[str, Any], criteria_data: Dict[str, Any]):
        """확인필요 문항을 인간 채점자가 보기 쉽게 요약 출력."""
        queue = grade_result.get("review_queue", [])
        if not queue:
            print("확인필요 문항 없음.")
            return
        print(f"\n[확인필요 문항 {len(queue)}건] — 1차 인식 결과를 보고 최종 결정하세요.")
        print("=" * 70)
        for q in queue:
            item_no = q["item_no"]
            print(f"\n문항 {item_no}  (배점 {q['max_score']}점 / 유형 {q['item_type']})")
            print(f"  1차 인식 결과(원문) : {q['preliminary_answer']!r}")
            print(f"  모범 정답(참조)     : {q['correct_answer']!r}")
            if q.get("rubric"):
                print(f"  루브릭              : {q['rubric']}")
            print("-" * 70)

    # ------------------------------------------------------------------
    # 일괄 처리 (병렬)
    # ------------------------------------------------------------------
    def process_batch(
        self,
        sheet_entries: List[tuple],
        criteria_data: Dict[str, Any],
        default_department: str = "",
    ) -> List[Dict[str, Any]]:
        """
        [(file_content, filename), ...] 목록을 병렬로 파싱 + 채점하여 결과 반환.
        sheet_entries 예시: [(content1, 'a1.png'), (content2, 'a2.png'), ...]
        각 결과에는 review_queue가 포함되며, 불확실한 문항은 0점 + 확인필요 처리됨.
        """
        def _one(entry):
            content, filename = entry
            info = self.parse_student_sheet(content, filename, criteria_data, default_department)
            result = self.grade_student(info, criteria_data)
            result["source_file"] = filename
            return result

        results = []
        if self.parallel_workers > 1 and len(sheet_entries) > 1:
            with ThreadPoolExecutor(max_workers=self.parallel_workers) as pool:
                futures = [pool.submit(_one, e) for e in sheet_entries]
                for f in as_completed(futures):
                    results.append(f.result())
        else:
            for e in sheet_entries:
                results.append(_one(e))
        return results


# ==================================================================
# 사용 예시 (파일 하단 주석)
# ==================================================================
if __name__ == "__main__":
    # 예시 실행용 — 실제 사용 시 UpstageClient 초기화 및 criteria_data를 준비하세요.
    #
    # client = UpstageClient(api_key="...")
    #
    # grader = StudentGrader(
    #     client,
    #     max_retries=3,
    #     parallel_workers=4,
    #     short_answer_llm_rejudge=False,   # 단답형 자모/철자 혼동 2차 판별 필요 시 True
    #     crop_answer_ratio=0.30,            # 답안지 배치에 맞게 조정 (질문 상단 30% 제거 예시)
    #     target_min_px=2000,
    #     deskew_verify_sample="sample_answer_sheet.png",  # 기울기 방향 검증용 샘플 (선택)
    #     enable_english_cleanup=True,
    # )
    #
    # criteria_data = {
    #     "items": [
    #         {"item_no": "1-1", "type": "단답형", "score": 4, "correct_answer": "문제", "rubric": "..."},
    #         {"item_no": "1-2", "type": "단답형", "score": 4, "correct_answer": "추상화", "rubric": "..."},
    #         {"item_no": "2", "type": "객관식", "score": 4, "correct_answer": "2", "rubric": "..."},
    #         # ...
    #     ]
    # }
    #
    # # 1부 처리
    # info = grader.parse_student_sheet(content, "s001.png", criteria_data, "일반")
    # grade_result = grader.grade_student(info, criteria_data)
    #
    # print(f"총점(1차, 불확실은 0점 처리): {grade_result['total_score']}")
    # print(f"확인필요 문항 수: {len(grade_result.get('review_queue', []))}")
    #
    # # 인간 채점자 확인 단계
    # grader.print_review_queue(grade_result, criteria_data)
    #
    # # 예: 문항 1-2를 인간이 보고 확정
    # grader.confirm_answer(
    #     grade_result,
    #     item_no="1-2",
    #     final_score=4.0,
    #     final_status="O",
    #     final_feedback="OCR 불확실였으나 검토 결과 정답",
    # )
    # print(f"확정 후 총점: {grade_result['total_score']}")
    # print("done")
    pass