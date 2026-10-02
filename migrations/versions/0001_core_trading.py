"""Initial core and trading tables.

Revision ID: 0001
"""
from alembic import op
from migrations.schema_v0001 import metadata, writer

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())
    op.bulk_insert(writer, [{"id": 1, "revision": 0}])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION quantro_check_ledger_balance() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF EXISTS (
                    SELECT currency FROM ledger_entries
                    WHERE transaction_id = NEW.transaction_id
                    GROUP BY currency HAVING SUM(signed_amount) <> 0
                ) THEN
                    RAISE EXCEPTION 'Unbalanced ledger transaction';
                END IF;
                RETURN NEW;
            END $$
        """)
        op.execute("""
            CREATE CONSTRAINT TRIGGER quantro_ledger_balance
            AFTER INSERT ON ledger_entries DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION quantro_check_ledger_balance()
        """)
        op.execute("""
            CREATE FUNCTION quantro_history_is_append_only() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'Financial history is append-only';
            END $$
        """)
        for table in ("ledger_entries", "ledger_transactions", "strategy_assignment_revisions", "strategy_evaluations"):
            op.execute(f"""
                CREATE TRIGGER quantro_append_only BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION quantro_history_is_append_only()
            """)


def downgrade():
    raise RuntimeError("Financial history cannot be dropped automatically; restore a backup explicitly")
