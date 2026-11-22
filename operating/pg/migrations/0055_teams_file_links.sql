-- 0055_teams_file_links: the file a Teams message shares, linked to the captured SharePoint
-- file it is (operating/pg/teams_file_links.py write_links). One row per derived link: the
-- message's and the file's handles and object ids, the address both name (url), how it was
-- derived (via: url_exact = the Teams attachment's content_url equals the capture's webUrl;
-- url_owner_path = equals the OneDrive owner of a Doc.aspx webUrl + the file's stored path;
-- exact string equality after one percent-decoding, a name or address two files hold links
-- nothing), and the door columns of BOTH ends (the message row's and the file row's
-- audience_keys, rec_marks, col_marks, ai_ok). The served reader view
-- (<schema>_sql.teams_message_files) shows a row only when the reader reads BOTH ends in
-- their own guarded views now. Declared here, not by the writer (the declared-schema rule);
-- the writer's per-run stage is its own UNLOGGED table. A database without the biz schema is
-- left alone. Undo: DROP VIEW biz_sql.teams_message_files; DROP TABLE biz.teams_file_links.
DO $migration$
BEGIN
    IF to_regnamespace('biz') IS NULL THEN
        RETURN;
    END IF;
    CREATE TABLE IF NOT EXISTS biz.teams_file_links (
        message_handle    text NOT NULL,
        file_handle       text NOT NULL,
        message_object_id text NOT NULL,
        file_object_id    text NOT NULL,
        url               text NOT NULL,
        via               text NOT NULL,
        msg_audience_keys text[],
        msg_rec_marks     text[],
        msg_col_marks     jsonb,
        msg_ai_ok         boolean,
        file_audience_keys text[],
        file_rec_marks    text[],
        file_col_marks    jsonb,
        file_ai_ok        boolean,
        PRIMARY KEY (message_handle, file_handle)
    );
    CREATE INDEX IF NOT EXISTS teams_file_links_file ON biz.teams_file_links (file_handle);
END
$migration$;
