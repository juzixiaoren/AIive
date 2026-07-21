"""测试 CapabilityPlanner.analyze_goal 的 prompt 渲染与 JSON 兜底解析。"""

from aiive.core.llm_client import FakeLLMClient
from aiive.mcp.capability_planner import CapabilityPlanner


class TestAnalyzeGoal:
    """验证 analyze_goal 的模板渲染不因裸花括号抛 KeyError，且解析健壮。"""

    def test_prompt_rendering_does_not_raise_keyerror(self):
        """含字面量 JSON 示例的模板不应因 str.format 而失败。"""
        fake = FakeLLMClient(
            fixed_content='{"goal_summary": "读文件", "missing_capability_type": "filesystem", '
            '"search_keywords": ["fs"], "risk_tolerance": "low", "reasoning": "需要访问文件"}'
        )
        planner = CapabilityPlanner(fake)

        result = planner.analyze_goal("帮我读取本地文件")

        assert result["missing_capability_type"] == "filesystem"
        assert result["risk_tolerance"] == "low"

    def test_prompt_contains_rendered_goal_and_json_example(self):
        """渲染后的 prompt 应替换 goal 占位符，且保留 JSON 示例中的花括号。"""
        fake = FakeLLMClient(
            fixed_content='{"goal_summary": "x", "missing_capability_type": "other", '
            '"search_keywords": [], "risk_tolerance": "medium", "reasoning": ""}'
        )
        planner = CapabilityPlanner(fake)

        planner.analyze_goal("我的独特目标标记XYZ")

        sent_prompt = fake._call_history[0]["messages"][0]["content"]
        assert "我的独特目标标记XYZ" in sent_prompt
        assert "{goal}" not in sent_prompt
        assert '"goal_summary": "一句话总结"' in sent_prompt

    def test_malformed_json_repaired(self):
        """模型返回带尾逗号的非法 JSON 时应经 repair_json 兜底解析成功。"""
        fake = FakeLLMClient(
            fixed_content='{"goal_summary": "y", "missing_capability_type": "github", '
            '"search_keywords": ["gh"], "risk_tolerance": "high", "reasoning": "r",}'
        )
        planner = CapabilityPlanner(fake)

        result = planner.analyze_goal("访问 GitHub 仓库")

        assert result["missing_capability_type"] == "github"
        assert result["risk_tolerance"] == "high"
