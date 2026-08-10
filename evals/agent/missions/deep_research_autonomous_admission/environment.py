"""Controlled Environment for autonomous Deep Research admission."""

from evals.agent.deep_research_support import DeepResearchMissionEnvironment


class DeepResearchAutonomousAdmissionEnvironment(DeepResearchMissionEnvironment):
    expected_objective_terms = ("Hermes", "OpenClaw", "LangGraph")
    expected_deliverable_terms = ("研究报告", "执行摘要", "对比矩阵")
    expected_constraint_terms = ("2024", "事实", "推断")
    minimum_research_questions = 3
    minimum_source_target = 15
