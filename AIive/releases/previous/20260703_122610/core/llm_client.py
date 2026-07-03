"""
LLM Client模块

提供LLM API调用能力，支持OpenAI-compatible API格式，支持流式输出。
"""

import os
import json
import requests
from typing import List, Dict, Any, Optional, Generator


class LLMClient:
    """LLM客户端"""
    
    def __init__(self):
        """初始化LLM客户端，从环境变量读取配置"""
        self.base_url = os.getenv("LLM_BASE_URL", "")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")
        
        if not self.base_url or not self.api_key:
            raise ValueError("LLM_BASE_URL 和 LLM_API_KEY 环境变量必须配置")
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        response_format: Optional[str] = None,
        stream: bool = False
    ) -> str:
        """
        调用LLM API
        
        Args:
            messages: 消息列表，格式 [{"role": "user", "content": "..."}]
            temperature: 温度参数
            response_format: 响应格式，可选 "json_object"
            stream: 是否使用流式输出
            
        Returns:
            str: LLM回复内容
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream
        }
        
        if response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        
        try:
            response = requests.post(
                self.base_url,
                headers=headers,
                json=payload,
                timeout=120,
                stream=stream
            )
            response.raise_for_status()
            
            if stream:
                return self._process_stream_response(response)
            else:
                result = response.json()
                
                if "choices" in result and len(result["choices"]) > 0:
                    return result["choices"][0]["message"]["content"]
                else:
                    raise ValueError(f"LLM返回格式异常: {result}")
                
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"LLM API调用失败: {e}")
    
    def _process_stream_response(self, response) -> str:
        """
        处理流式响应
        
        Args:
            response: requests响应对象
            
        Returns:
            str: 完整的回复内容
        """
        full_content = ""
        
        for line in response.iter_lines():
            if not line:
                continue
            
            line = line.decode('utf-8')
            
            # 跳过SSE前缀
            if line.startswith('data: '):
                line = line[6:]
            
            # 检查结束标志
            if line.strip() == '[DONE]':
                break
            
            try:
                data = json.loads(line)
                
                if "choices" in data and len(data["choices"]) > 0:
                    delta = data["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    
                    if content:
                        full_content += content
                        # 实时打印到控制台
                        print(content, end="", flush=True)
                        
            except json.JSONDecodeError:
                continue
        
        print()  # 换行
        return full_content
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        stream: bool = False
    ) -> Dict[str, Any]:
        """
        调用LLM API并返回JSON格式结果
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            stream: 是否使用流式输出
            
        Returns:
            dict: 解析后的JSON结果
        """
        response = self.chat(messages, temperature, response_format="json_object", stream=stream)
        
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            # 尝试从响应中提取JSON
            return self._extract_json(response)
    
    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        response_format: Optional[str] = None
    ) -> Generator[str, None, None]:
        """
        流式调用LLM API
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            response_format: 响应格式
            
        Yields:
            str: 每个token的内容
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True
        }
        
        if response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        
        try:
            response = requests.post(
                self.base_url,
                headers=headers,
                json=payload,
                timeout=120,
                stream=True
            )
            response.raise_for_status()
            
            for line in response.iter_lines():
                if not line:
                    continue
                
                line = line.decode('utf-8')
                
                # 跳过SSE前缀
                if line.startswith('data: '):
                    line = line[6:]
                
                # 检查结束标志
                if line.strip() == '[DONE]':
                    break
                
                try:
                    data = json.loads(line)
                    
                    if "choices" in data and len(data["choices"]) > 0:
                        delta = data["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        
                        if content:
                            yield content
                            
                except json.JSONDecodeError:
                    continue
                    
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"LLM API调用失败: {e}")
    
    def _extract_json(self, text: str) -> Dict[str, Any]:
        """
        从文本中提取JSON
        
        Args:
            text: 包含JSON的文本
            
        Returns:
            dict: 提取的JSON
        """
        # 尝试找到JSON块
        import re
        json_patterns = [
            r'```json\s*(.*?)\s*```',
            r'\{.*\}',
            r'\[.*\]'
        ]
        
        for pattern in json_patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1) if '```' in pattern else match.group())
                except json.JSONDecodeError:
                    continue
        
        raise ValueError(f"无法从响应中提取JSON: {text[:200]}...")
    
    def is_configured(self) -> bool:
        """
        检查LLM是否已配置
        
        Returns:
            bool: 是否已配置
        """
        return bool(self.base_url and self.api_key)
    
    def dry_run(self) -> Dict[str, Any]:
        """
        测试LLM连接
        
        Returns:
            dict: 测试结果
        """
        if not self.is_configured():
            return {
                "success": False,
                "message": "LLM未配置"
            }
        
        try:
            response = self.chat([
                {"role": "user", "content": "Hello, please respond with 'OK'."}
            ], temperature=0)
            
            return {
                "success": True,
                "message": "LLM连接正常",
                "model": self.model,
                "response": response[:100]
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"LLM连接失败: {e}"
            }


# 全局LLM客户端实例
_llm_client = None


def get_llm_client() -> LLMClient:
    """
    获取全局LLM客户端实例
    
    Returns:
        LLMClient: LLM客户端实例
    """
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
