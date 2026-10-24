from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config
from .models import Decision, Paper, Pick, Summary
from .report import write_report


def demo(config: Config):
    examples = [
        (
            "Teaching Language Agents to Verify Tool Results",
            "让智能体检查工具的返回结果",
            "在执行下一步操作前，让语言智能体先检查工具返回内容是否可靠。",
            "多步任务中的工具错误会在后续操作中累积。",
            "增加独立的验证步骤，对工具输出与任务目标的一致性进行检查。",
            "这是虚构示例，没有真实实验结果。",
        ),
        (
            "Adaptive Memory Budgets for Long-Context Inference",
            "为长上下文推理动态分配内存",
            "根据当前任务需要，动态调整上下文缓存的内存预算。",
            "长上下文推理的缓存开销随输入长度持续增长。",
            "按信息的重要程度保留缓存，并在预算内重新分配内存。",
            "这是虚构示例，没有真实加速比或内存节省数据。",
        ),
        (
            "A Test Suite for Reliable Research Assistants",
            "研究助手可靠性测试集",
            "用包含工具异常和证据冲突的任务检查研究助手的可靠性。",
            "常规评测难以揭示助手面对不完整或矛盾信息时的表现。",
            "构建多步研究任务，并分别评估证据追踪、错误恢复和引用准确性。",
            "这是虚构示例，不对应真实数据集。",
        ),
    ]
    picks = []
    for index, (title, translated, tldr, problem, method, results) in enumerate(examples):
        score = 0.96 - index * 0.05
        paper = Paper(
            id=f"demo-{index + 1}",
            title=title,
            abstract=f"Synthetic demo. {tldr}",
            authors=["Demo Research Group"],
            categories=["cs.AI"],
            published="2026-10-24",
        )
        decision = Decision(
            score=score,
            interests={config.interests[index % len(config.interests)].id: score},
            exclusions={},
            excluded=False,
            model="offline-demo-not-jev",
        )
        summary = Summary(
            title_zh=translated,
            tldr=tldr,
            problem=problem,
            method=method,
            results=results,
            limitations="演示内容仅用于预览邮件布局，不应用作论文信息或模型效果证据。",
        )
        picks.append(Pick(paper, decision, summary))
    output = config.output_dir / "demo"
    write_report(
        output,
        config,
        picks,
        datetime.now(ZoneInfo(config.timezone)),
        {"fetched": 320, "selected_unique": 3, "jev_calls": 0, "summary_calls": 0},
        demo=True,
    )
    return output
