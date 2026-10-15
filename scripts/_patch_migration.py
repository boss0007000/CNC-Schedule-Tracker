"""Re-append the Postgres audit-log trigger after regenerating 0001 (pre-release only)."""
p = "alembic/versions/0001_initial_schema.py"
s = open(p, encoding="utf-8").read()
if "audit_log_immutable" not in s:
    trigger = '''
    if op.get_bind().dialect.name == "postgresql":
        # Defence in depth: the audit log is append-only at the database level too.
        op.execute("""
        CREATE FUNCTION audit_log_immutable() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'audit_log is append-only'; END; $$ LANGUAGE plpgsql;
        """)
        op.execute("""
        CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
        """)

'''
    i = s.index("def downgrade")
    s = s[:i].rstrip() + "\n" + trigger + "\n" + s[i:]
    open(p, "w", encoding="utf-8").write(s)
