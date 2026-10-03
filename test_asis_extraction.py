import json
from dotenv import load_dotenv
from upstage_client import UpstageClient
from ocr_grader import StudentGrader

def test_asis_extraction():
    load_dotenv(override=True)
    client = UpstageClient()
    
    # 가상의 OCR 인식 텍스트 (학생이 오타를 작성한 상황)
    sample_ocr_text = """
    [답안지]
    학과: 스마트소프트웨어과
    학번: 2024101
    성명: 이오타
    
    1번 답: 3번
    2번 답: 광합선 작용을 통해 산소를 방출함 (오타: 광합선)
    3번 답: 알골리즘 (오타: 알골리즘)
    """
    
    item_keys = ["1", "2", "3"]
    prompt = f"[답안지 파싱 내용]\n{sample_ocr_text}"
    system_inst = StudentGrader.EXTRACT_PROMPT.format(item_list=", ".join(item_keys))
    
    print("Upstage Solar LLM에 오타 추출 테스트 전송 중...")
    res = client.call_solar(
        prompt=prompt,
        system_instruction=system_inst,
        temperature=0.0,
        response_format_json=True
    )
    
    result = json.loads(res)
    print("\n--- [LLM 추출 결과 JSON] ---")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    ans2 = result["answers"]["2"]
    ans3 = result["answers"]["3"]
    
    print("\n--- [검증 결과] ---")
    print(f"2번 문항 학생 답안: '{ans2}' -> '광합선' 오타 보존 여부:", "광합선" in ans2)
    print(f"3번 문항 학생 답안: '{ans3}' -> '알골리즘' 오타 보존 여부:", "알골리즘" in ans3)
    
    assert "광합선" in ans2, "오타가 정답(광합성)으로 임의 교정되면 안 됩니다!"
    assert "알골리즘" in ans3, "오타가 정답(알고리즘)으로 임의 교정되면 안 됩니다!"
    print("\n✅ 확인 완료: 학생이 적은 오탈자가 정답으로 왜곡되지 않고 원문 그대로(as-is) 정확히 추출되었습니다!")

if __name__ == "__main__":
    test_asis_extraction()
