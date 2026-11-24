CREATE TABLE public.pages (id integer PRIMARY KEY, source_id text, slug text, type text, page_kind text, title text, compiled_truth text, timeline text, frontmatter jsonb, content_hash text, created_at timestamptz, updated_at timestamptz, deleted_at timestamptz, effective_date timestamptz, effective_date_source text, source_path text, source_uri text, source_kind text, ingested_via text, ingested_at timestamptz);
CREATE TABLE public.page_versions (id integer, page_id integer, compiled_truth text, frontmatter jsonb, snapshot_at timestamptz);
CREATE TABLE public.page_aliases (id bigint, source_id text, alias_norm text, slug text, created_at timestamptz);
CREATE TABLE public.slug_aliases (id bigint, source_id text, alias_slug text, canonical_slug text, notes text, created_at timestamptz);
CREATE TABLE public.links (id integer, from_page_id integer, to_page_id integer, link_type text, context text, link_source text, link_kind text, origin_page_id integer, origin_field text, resolution_type text, created_at timestamptz);
CREATE TABLE public.tags (id integer, page_id integer, tag text);
CREATE TABLE public.timeline_entries (id integer, page_id integer, date date, source text, summary text, detail text, event_page_id integer, created_at timestamptz);
CREATE TABLE public.sources (id text, name text);
INSERT INTO sources VALUES ('fake-source', 'Invented knowledge');
INSERT INTO pages (id,source_id,slug,type,page_kind,title,compiled_truth,timeline,frontmatter,created_at,updated_at,effective_date) VALUES
 (1,'fake-source','orchard','concept','note','Invented orchard','Apple trees.','A tree was planted.','{"invented":true}','2020-01-02T03:00:00+00','2021-01-01T00:00:00+00','2020-01-01T00:00:00+00'),
 (2,'fake-source','garden','place','note','Invented garden','Flowers.','','{}','2020-02-01T00:00:00+00','2021-01-01T00:00:00+00',NULL),
 (3,'fake-source','retired','note','note','Invented retired note','Retired.','','{}','2020-03-01T00:00:00+00','2021-01-01T00:00:00+00',NULL);
UPDATE pages SET deleted_at='2022-01-01T00:00:00Z' WHERE id=3;
INSERT INTO page_aliases VALUES (1,'fake-source','fruit trees','orchard','2020-01-01T00:00:00Z');
INSERT INTO slug_aliases VALUES (1,'fake-source','old-orchard','orchard','Invented alias','2020-01-01T00:00:00Z');
INSERT INTO tags VALUES (1,1,'invented');
INSERT INTO links (id,from_page_id,to_page_id,link_type,context) VALUES (1,1,2,'mentions','Invented garden context');
INSERT INTO timeline_entries (id,page_id,date,source,summary,detail) VALUES (1,1,'2020-01-01','invented','Planted a tree.','An invented apple tree.');
-- Legacy marker: pages flagged with GBRAIN_LEGACY_MARKER (default source_copy)
-- in frontmatter, a page flag column or a tag go to gbrain.legacy; invented
-- examples are in legacy.sql, loaded only by the legacy tests.
