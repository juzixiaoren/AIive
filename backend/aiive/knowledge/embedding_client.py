"""
向量嵌入客户端模块。

提供文本转向量（embedding）的抽象接口及伪实现。
生产环境应替换为真实的嵌入服务（如 OpenAI text-embedding-3-small），
伪实现仅用于单元测试和本地开发。
"""

import hashlib
from abc import ABC, abstractmethod
from typing import override

EMBEDDING_DIM = 384  # 向量维度


class EmbeddingClient(ABC):
    """向量嵌入客户端抽象基类。"""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """
        将文本列表转换为向量列表。

        参数:
            texts: 待嵌入的文本列表。

        返回:
            嵌入向量列表，每个向量为 float 列表。
        """
        ...


class FakeEmbeddingClient(EmbeddingClient):
    """
    确定性伪嵌入客户端，仅供单元测试使用，禁止用于生产环境。

    使用 SHA-256 哈希生成向量，语义不同的文本会得到完全不同的向量。
    生产环境必须使用真实的嵌入服务（如 OpenAI text-embedding-3-small）。
    """

    @override
    def embed(self, texts: list[str]) -> list[list[float]]:
        """
        使用 SHA-256 哈希为每个文本生成确定性伪向量。

        参数:
            texts: 待嵌入的文本列表。

        返回:
            伪嵌入向量列表。
        """
        results = []
        for text in texts:
            h = hashlib.sha256(text.encode()).digest()
            vec = []
            for i in range(EMBEDDING_DIM):
                byte_idx = i % len(h)
                # 将字节值映射到 [-1, 1] 范围
                val = (h[byte_idx] / 255.0) * 2 - 1
                # 为每个维度添加微小偏移，增加区分度
                val += (i * 0.0001)
                vec.append(round(val, 6))
            results.append(vec)
        return results
