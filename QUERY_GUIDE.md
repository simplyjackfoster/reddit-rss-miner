# Query guide

Every row file the miner writes (batch `--rows`, crawl `--full-subreddit`) is newline-delimited
JSON with one schema. Convert to typed Parquet once, then query with SQL through DuckDB. No server,
no import step. Any shell with Python can do this, which makes the dataset usable by a person or an
agent without re-deriving the schema.

```bash
pip install duckdb                                     # or: pip install 'reddit-rss-miner[query]'
reddit-rss-miner --convert batch.jsonl crawl.jsonl --parquet rows.parquet     # merge N files into one
reddit-rss-miner --query "SELECT count(*) FROM rows" --from rows.parquet       # ad hoc SQL
```

`--from` accepts `.parquet` and `.jsonl` files together; they are unioned into one `rows` view.
Or from Python:

```python
from reddit_rss_miner import connect
con = connect(["rows.parquet"])
con.sql("SELECT matched_in, count(*) FROM rows GROUP BY 1").show()
```

## Schema

Types are declared, not inferred, so every file has exactly these columns and types even when a
column is empty in that file.

| column | type | meaning |
|---|---|---|
| `sub` | VARCHAR | subreddit the post lives in |
| `post_id` | VARCHAR | `t3_...` id of the thread |
| `post_title` | VARCHAR | thread title |
| `item_id` | VARCHAR | `t3_...` for the post row, `t1_...` for a comment |
| `item_type` | VARCHAR | `post` or `comment` |
| `author` | VARCHAR | username, NULL if deleted |
| `body` | VARCHAR | text with HTML stripped |
| `url` | VARCHAR | permalink |
| `updated` | VARCHAR | raw ISO-8601 string from the feed |
| `updated_at` | TIMESTAMPTZ | `updated` parsed; NULL when the feed had none |
| `links` | VARCHAR[] | outbound URLs the author wrote, or a link post's target |
| `images` | VARCHAR[] | image sources |
| `matched_terms` | VARCHAR[] | products this row mentions (batch mode); empty in crawl mode |
| `matched_in` | VARCHAR | `post`, `comment`, `thread_context`, or `dedicated_subreddit` |
| `snippet` | VARCHAR | text around the first mention (batch mode) |
| `author_flags` | VARCHAR[] | labels from `--flag-authors`, e.g. `official` |
| `source` | VARCHAR | file name the row came from |

Read `matched_in` before counting anything. `post` and `comment` rows passed the literal-mention
filter. `thread_context` rows did not mention the product themselves and exist only if the run used
`--thread-context`. `dedicated_subreddit` rows are unfiltered by design.

## Example queries

Rows per product, split by where the mention was found:

```sql
SELECT product, matched_in, count(*) AS n
FROM (SELECT unnest(matched_terms) AS product, matched_in FROM rows)
GROUP BY 1, 2 ORDER BY 1, 2;
```

Everything a company's own accounts said (after `--flag-authors official:acct1,acct2`):

```sql
SELECT updated_at, sub, author, left(body, 200) AS said, url
FROM rows WHERE list_contains(author_flags, 'official') ORDER BY updated_at DESC;
```

Threads where an official account replied, with the size of the thread:

```sql
SELECT post_id, any_value(post_title) AS title, count(*) AS rows_in_thread,
       sum(list_contains(author_flags, 'official')::int) AS official_replies
FROM rows GROUP BY post_id HAVING official_replies > 0 ORDER BY rows_in_thread DESC;
```

Mentions per month for one product:

```sql
SELECT date_trunc('month', updated_at) AS month, count(*) AS mentions
FROM rows WHERE list_contains(matched_terms, 'Things') AND matched_in IN ('post', 'comment')
GROUP BY 1 ORDER BY 1;
```

Rows mentioning two products in the same item (direct comparisons):

```sql
SELECT sub, left(body, 240) AS text, url
FROM rows WHERE list_contains(matched_terms, 'Things') AND list_contains(matched_terms, 'Obsidian');
```

Outbound links people share, by domain:

```sql
SELECT regexp_extract(link, '^https?://([^/]+)', 1) AS domain, count(*) AS n
FROM (SELECT unnest(links) AS link FROM rows)
GROUP BY 1 ORDER BY n DESC LIMIT 20;
```

Most active commenters in a dedicated subreddit crawl:

```sql
SELECT author, count(*) AS comments
FROM rows WHERE matched_in = 'dedicated_subreddit' AND item_type = 'comment' AND author IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC LIMIT 15;
```

Keyword search inside bodies (case-insensitive):

```sql
SELECT sub, author, left(body, 200) AS text, url
FROM rows WHERE body ILIKE '%refund%' OR body ILIKE '%cancel%' ORDER BY updated_at DESC;
```

Full-text search with ranking, when `ILIKE` is not enough:

```sql
INSTALL fts; LOAD fts;
CREATE TABLE r AS SELECT * FROM rows;
PRAGMA create_fts_index('r', 'item_id', 'body', 'post_title');
SELECT item_id, fts_main_r.match_bm25(item_id, 'sync conflict lost notes') AS score, left(body, 160) AS text
FROM r WHERE score IS NOT NULL ORDER BY score DESC LIMIT 20;
```

## Caveats that travel with the data

- No vote signal. RSS carries no scores, so every row weighs the same. Counts measure mentions, not agreement.
- A dedicated-subreddit crawl is the newest ~1,000 posts. Check `ceiling_suspected` in the crawl summary.
- Which posts Reddit's search surfaced is not controllable and varies between runs. The per-term
  `verdict`, `post_level_precision`, and discard counts in the batch `stats.json` are the disclosure.
- Comments are flat: no parent id, depth, or score. Threads can be regrouped by `post_id` only.
