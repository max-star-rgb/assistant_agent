"""Controlled Environment for a conflicting-evidence research plan."""

from evals.agent.deep_research_support import DeepResearchMissionEnvironment


class DeepResearchEvidencePlanEnvironment(DeepResearchMissionEnvironment):
    expected_objective_terms = ("AI", "编程 Agent", "交付效率")
    expected_deliverable_terms = ("研究报告", "claim-evidence", "局限性")
    expected_constraint_terms = ("冲突", "证据强度", "偏差")
    minimum_research_questions = 4
    minimum_source_target = 18
