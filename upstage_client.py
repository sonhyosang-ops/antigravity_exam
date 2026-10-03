import os
import io
import json
import requests
from typing import Optional, Dict, Any, List

def get_secret_api_key() -> str:
    """보안 강화를 위해 화면 노출 없이 환경 변수(.env) 또는 streamlit secrets에서만 안전하게 키를 조회합니다."""
    # 1. Streamlit secrets 확인
    try:
        import streamlit as st
        if hasattr(st, "secrets") and "UPSTAGE_API_KEY" in st.secrets:
            return st.secrets["UPSTAGE_API_KEY"].strip()
    except Exception:
        pass
        
    # 2. 로컬 환경 변수 (.env) 확인
    key = os.getenv("UPSTAGE_API_KEY", "")
    if not key:
        # 혹시 끝에 오타(예: UPSTAGE_API_KEYL 등)가 있더라도 찾아내기 위한 보조 로직
        for env_name, env_val in os.environ.items():
            if env_name.startswith("UPSTAGE_API_KEY") and env_val.startswith("up_"):
                key = env_val
                break
    return key.strip() if key else ""


class UpstageClient:
    """Upstage API Client for Document Parse and Solar LLM (화면 비노출 보안 설계)"""
    
    DOCUMENT_PARSE_URL = "https://api.upstage.ai/v1/document-ai/document-parse"
    SOLAR_CHAT_URL = "https://api.upstage.ai/v1/solar/chat/completions"
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or get_secret_api_key()
        if not self.api_key or self.api_key == "your_upstage_api_key_here":
            raise ValueError(
                "보안 설정 오류: Upstage API 키가 설정되지 않았습니다.\n"
                "프로젝트 루트의 '.env' 파일에 UPSTAGE_API_KEY=발급받은키 를 저장해주세요. (화면에는 노출되지 않습니다)"
            )
            
    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}"
        }

    def parse_document(self, file_content: bytes, filename: str = "document.pdf") -> Dict[str, Any]:
        """Upstage Document Parse API를 호출하여 문서의 구조화된 텍스트/표/HTML을 반환합니다."""
        headers = self._get_headers()
        files = {
            "document": (filename, file_content)
        }
        data = {
            "ocr": "force",
            "base64_encoding": "['table', 'figure']"
        }
        
        response = requests.post(
            self.DOCUMENT_PARSE_URL,
            headers=headers,
            files=files,
            data=data,
            timeout=120
        )
        
        if response.status_code != 200:
            raise Exception(f"Upstage Document Parse 호출 실패 ({response.status_code}): {response.text}")
            
        return response.json()

    def call_solar(self, prompt: str, system_instruction: str = "", temperature: float = 0.1, response_format_json: bool = False) -> str:
        """Upstage Solar LLM을 호출하여 채점 기준표 파싱 또는 채점을 수행합니다."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        
        payload: Dict[str, Any] = {
            "model": "solar-pro",
            "messages": messages,
            "temperature": temperature
        }        
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}
            
        response = requests.post(
            self.SOLAR_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=120
        )
        
        if response.status_code != 200:
            # solar-pro 실패 시 fallback으로 solar-mini 시도
            payload["model"] = "solar-mini"
            fallback_res = requests.post(
                self.SOLAR_CHAT_URL,
                headers=headers,
                json=payload,
                timeout=120
            )
            if fallback_res.status_code != 200:
                raise Exception(f"Upstage Solar API 호출 실패 ({response.status_code}): {response.text}")
            response = fallback_res

        result = response.json()
        return result["choices"][0]["message"]["content"]
