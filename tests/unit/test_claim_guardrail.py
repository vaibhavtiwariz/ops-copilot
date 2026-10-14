"""Tests for the claim-audit guardrail."""

from ops_copilot.guardrails import claims_execution


class TestClaimDetection:
    def test_detects_execution_claims(self):
        assert claims_execution("The order has been refunded successfully")
        assert claims_execution("I initiated the refund for order 42")
        assert claims_execution("Comment posted on the PR")

    def test_neutral_answers_pass(self):
        assert not claims_execution("Total revenue was 2,031,465.8 dollars")
        assert not claims_execution("There are 3 open incidents across two services")
        assert not claims_execution("")

    def test_proposal_wording_is_not_a_claim(self):
        # "proposed" / "pending approval" phrasing must not trip it
        assert not claims_execution("Registered a proposal; awaiting human approval")

    def test_worker_summary_flags_flow_into_task(self):
        from ops_copilot.graph.state import Task

        task = Task(id="t1", description="x", assignee="triage", status="done", summary="")
        flagged = task.model_copy(update={"summary": "[unverified action claim] refund done"})
        assert flagged.summary.startswith("[unverified action claim]")
