-- Autovacuum of the big ontology tables at 1% change instead of the default 20%.
-- A delta apply or a step's bulk apply deletes and inserts millions of rows; at 20% of
-- value_cells (520M rows) autovacuum never started (11-11: 42.6M dead rows, its visibility
-- map at 41%), and link_rows' index-only reads turned into heap reads (a hub's link count
-- took 6-9 s at a 29% map). Inserts count too (insert_scale_factor), so the map is set
-- again after an insert-only apply. A cost limit of 1000 lets a run over a big table end
-- within a cycle or two. ALTER TABLE ... SET only stores options (no rewrite).
ALTER TABLE ontology.link_rows SET (autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_vacuum_insert_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.01,
    autovacuum_vacuum_cost_limit = 1000);
ALTER TABLE ontology.value_cells SET (autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_vacuum_insert_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.01,
    autovacuum_vacuum_cost_limit = 1000);
ALTER TABLE ontology.object_versions SET (autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_insert_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE ontology.value_sources SET (autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_insert_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE ontology.objects SET (autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_insert_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE ontology.identifiers SET (autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_insert_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE ontology.event_objects SET (autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_insert_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
