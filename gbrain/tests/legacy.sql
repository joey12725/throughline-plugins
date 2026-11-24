-- Invented legacy bulk pages (loaded by test_gbrain_legacy.py on top of source.sql).
-- The legacy marker (GBRAIN_LEGACY_MARKER, default source_copy) is found in
-- the live page's frontmatter (a key set to true, or a value or list item equal
-- to the marker), in a page flag column (page_kind, type, source_kind,
-- ingested_via) or as a tag. Both frontmatter and flag forms are below.
INSERT INTO pages (id,source_id,slug,type,page_kind,title,compiled_truth,timeline,frontmatter,created_at,updated_at,effective_date) VALUES
 (4,'fake-source','legacy-note','document','note','Invented legacy note','Quillberry legacy copy text.','','{"source_copy":true}','2020-04-01T00:00:00+00','2021-04-01T00:00:00+00',NULL),
 (5,'fake-source','legacy-flag','document','source_copy','Invented flagged copy','Quillberry flagged copy.','','{}','2020-05-01T00:00:00+00','2021-05-01T00:00:00+00',NULL),
 (6,'fake-source','invented-person','person','note','Invented Person','An invented person page that is long enough to be oversized.','','{}','2020-06-01T00:00:00+00','2021-06-01T00:00:00+00',NULL),
 (7,'fake-source','legacy-deleted','document','note','Invented deleted copy','Deleted copy.','','{"tags":["source_copy"]}','2020-07-01T00:00:00+00','2021-07-01T00:00:00+00',NULL);
UPDATE pages SET deleted_at='2022-01-01T00:00:00Z' WHERE id=7;
UPDATE pages SET frontmatter='{"throughline_sources":[{"channel":"gbrain.pages","external_id":"fake-source:legacy-note"}]}' WHERE id=2;
INSERT INTO timeline_entries (id,page_id,date,source,summary,detail) VALUES
 (2,5,'2020-05-05','invented','Quillberry legacy event.','An invented legacy event.'),
 (3,7,'2020-07-07','invented','Deleted legacy event.','An invented deleted event.');
INSERT INTO links (id,from_page_id,to_page_id,link_type,context) VALUES
 (2,1,4,'mentions','Live to legacy'),
 (3,4,1,'Mention','Legacy to live'),
 (4,5,4,'→','Symbol-only type'),
 (5,2,5,'invented_retired_kind','Retired type');
