"""Controlled Environment for Deep Research submission grounding."""

from evals.agent.deep_research_support import DeepResearchMissionEnvironment


class DeepResearchConstraintGroundingEnvironment(DeepResearchMissionEnvironment):
    expected_objective_terms = ("中国", "LLM Agent", "可观测", "评测")
    expected_deliverable_terms = ("选型报告", "风险清单", "落地建议")
    expected_constraint_terms = ("私有化", "官方文档", "待确认")
    minimum_research_questions = 4
    minimum_source_target = 12
