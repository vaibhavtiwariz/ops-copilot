import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ops_copilot.guardrails import (
    BudgetExceeded,
    SqlPolicyError,
    check_budget,
    enforce_read_only,
    estimate_tokens,
    first_keyword,
    mask_pii,
    split_statements,
    tokens_of,
    write_target,
)


class TestReadOnlyPolicy:
    def test_plain_select_passes(self):
        sql = "select * from orders where status = 'paid'"
        assert enforce_read_only(sql).lower().startswith("select")

    def test_leading_whitespace_and_semicolon(self):
        sql = "  SELECT 1;"
        assert enforce_read_only(sql) == "SELECT 1"

    def test_multi_statement_rejected(self):
        with pytest.raises(SqlPolicyError, match="exactly one"):
            enforce_read_only("select 1; drop table users")

    def test_delete_rejected_even_with_comment_noise(self):
        with pytest.raises(SqlPolicyError):
            enforce_read_only("-- cleanup\nDELETE FROM incidents")

    def test_cte_wrapped_write_rejected(self):
        with pytest.raises(SqlPolicyError):
            enforce_read_only("with x as (select 1) delete from orders")

    def test_explain_analyze_on_write_rejected(self):
        with pytest.raises(SqlPolicyError):
            enforce_read_only("explain analyze update customers set plan='growth' where id=1")

    @pytest.mark.parametrize(
        "payload",
        [
            "select pg_sleep(10)",
            "select pg_read_file('/etc/passwd')",
            "select * from dblink('host=x dbname=y', 'select 1')",
            "select lo_import('/tmp/x')",
        ],
    )
    def test_denied_functions(self, payload):
        with pytest.raises(SqlPolicyError, match="denied by policy"):
            enforce_read_only(payload)

    def test_function_name_inside_string_literal_is_fine(self):
        # The literal mentions pg_sleep but never calls it.
        assert enforce_read_only("select 'pg_sleep(x)' as note") == "select 'pg_sleep(x)' as note"

    def test_empty_statement(self):
        with pytest.raises(SqlPolicyError):
            enforce_read_only("; ; -- nothing")


class TestSelectShapedWrites:
    """S5: read-shaped statements that smuggle writes in their body."""

    @pytest.mark.parametrize(
        "payload",
        [
            # the three shapes from the finding
            "select * into tmp_leak from users",
            "select * from orders for update",
            "select * from (delete from orders returning *) d",
            # H4's write-CTE shapes still blocked
            "with a as (select 1), b as (delete from scratch_notes returning *) select * from b",
            "with a as (update orders set x=1 returning *) select * from a",
            "with a as (insert into t select 1 returning *) select * from a",
            "with x as (select 1) delete from orders",
            "WITH D AS (DELETE FROM users RETURNING *) SELECT * FROM d",
            # own variants: nested subqueries, mixed case, schema-qualified INTO
            "select * from (select * from (update orders set x=1 returning *) u) v",
            "SELECT * INTO audit.tmp_leak FROM users",
            "select * from orders for no key update",
            "select * from (insert into scratch_notes select 1 returning *) s",
            "with x as (select 1) select * into tmp2 from x",
            "SeLeCt * FrOm (DeLeTe FrOm orders returning *) d",
        ],
    )
    def test_write_shaped_selects_rejected(self, payload):
        with pytest.raises(SqlPolicyError):
            enforce_read_only(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            "select * from orders where status = 'paid'",
            "with a as (select 1), b as (select 2 from a) select * from b",
            "select 'delete from orders' as note",
            "select * from t where note = 'insert into x values (1)'",
            "select updated_at, created_at from orders",
            "select 1 -- delete everything",
            "select * from (select id from orders) o where o.id > 0",
            "explain select * from orders",
            "table orders",
        ],
    )
    def test_legit_reads_still_pass(self, payload):
        assert enforce_read_only(payload)


def test_split_statements_trailing_semicolons():
    assert split_statements("select 1;;\n") == ["select 1"]


def test_first_keyword_variants():
    assert first_keyword("( select 1 )") == "select"
    assert first_keyword("TABLE orders") == "table"


def test_write_target_detection():
    target = write_target("UPDATE incidents SET status='resolved' WHERE id = 3")
    assert target.action == "update"
    assert target.table == "incidents"
    assert target.verbs == ("update",)


def test_write_target_unknown_when_no_write_verb():
    target = write_target("select 1")
    assert target.action == "unknown"
    assert target.verbs == ()


def test_write_target_mixed_on_hidden_second_verb():
    # The S8 case: start-anchored labeling used to call this a benign insert.
    target = write_target("insert into audit_log select * from (delete from orders returning *) d")
    assert target.action == "mixed"
    assert set(target.verbs) == {"insert", "delete"}


def test_write_target_scans_subqueries_and_ctes():
    target = write_target("with d as (delete from orders returning *) select * from d")
    assert target.action == "delete"
    assert target.table == "orders"
    # SELECT ... INTO is a read-path shape, not a write verb for labeling.
    assert write_target("SELECT * INTO reporting.tmp FROM users").action == "unknown"


class TestPiiMasking:
    def test_email(self):
        out = mask_pii("contact jane.doe@corp.example about it")
        assert "jane.doe@corp.example" not in out
        assert "[redacted]" in out

    def test_phone(self):
        out = mask_pii("call +1 415-555-1234 now")
        assert "415-555-1234" not in out

    def test_card_number(self):
        out = mask_pii("card was 4111 1111 1111 1111 btw")
        assert "4111" not in out

    def test_ssn(self):
        out = mask_pii("ssn 123-45-6789")
        assert "6789" not in out


class TestBudget:
    def test_estimate_tokens_minimum(self):
        assert estimate_tokens("") >= 1

    def test_usage_metadata_preferred(self):
        msg = AIMessage(
            content="hi",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
        assert tokens_of([msg]) == 15

    def test_falls_back_to_chars(self):
        long = HumanMessage(content="x" * 400)
        assert tokens_of([long]) == 100

    def test_check_budget_raises(self):
        msgs = [AIMessage(content="x" * 200)]
        with pytest.raises(BudgetExceeded):
            check_budget(msgs, limit=10)

    def test_under_budget_ok(self):
        check_budget([HumanMessage(content="hello")], limit=10_000)
