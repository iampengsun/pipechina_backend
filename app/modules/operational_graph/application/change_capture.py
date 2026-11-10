"""Database triggers enqueue relation changes in the same transaction as business writes."""

from sqlalchemy import event

from app.shared.db import Base

SOURCE_TABLES = (
    "operational_stations",
    "operational_equipment",
    "equipment_aliases",
    "failure_modes",
    "equipment_associations",
    "production_events",
    "event_source_links",
    "agent_runs",
    "agent_evidence",
    "workflow_runs",
    "hazards",
    "hazard_work_order_links",
    "hazard_rectifications",
    "hazard_acceptances",
    "work_orders",
    "work_order_resolutions",
    "work_order_acceptances",
    "experience_memories",
    "knowledge_versions",
    "knowledge_documents",
    "knowledge_bases",
    "knowledge_runs",
    "meetings",
    "meeting_knowledge_publications",
    "audio_records",
    "manual_operation_records",
    "inspection_records",
    "inspection_findings",
)


def capture_sql():
    yield """CREATE OR REPLACE FUNCTION capture_business_graph_change() RETURNS trigger
    LANGUAGE plpgsql AS $$ BEGIN
      INSERT INTO business_graph_outbox(source_table,record_id)
      VALUES (TG_TABLE_NAME, COALESCE(NEW.id,OLD.id)::text);
      IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END $$"""
    for table in SOURCE_TABLES:
        yield f'DROP TRIGGER IF EXISTS business_graph_change ON "{table}"'
        yield (
            f'CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()"
        )


def install_change_capture(target, connection, **kwargs):
    if connection.dialect.name == "postgresql":
        for statement in capture_sql():
            connection.exec_driver_sql(statement)


def register_change_capture():
    if not event.contains(Base.metadata, "after_create", install_change_capture):
        event.listen(Base.metadata, "after_create", install_change_capture)
