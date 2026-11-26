-- SharePoint folder creation and targeted document upload studio write classes (append-only after 0067).
INSERT INTO assist.write_classes (system, operation, class, b_rule, why) VALUES
  ('sharepoint', 'create_folder', 'A', '', 'reversible'),
  ('sharepoint', 'upload_document', 'B', '', 'overwrites a document'),
  ('sharepoint', 'put_file', 'B', '', 'overwrites a document')
ON CONFLICT (system, operation) DO UPDATE
SET class = EXCLUDED.class, b_rule = EXCLUDED.b_rule, why = EXCLUDED.why;
