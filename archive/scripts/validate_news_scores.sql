-- ============================================================================
-- validate_news_scores.sql
--
-- Score-vs-forward-return validation for the LLM news analyst (shadow workflow).
-- Discriminating test for docs/findings/news_attribution_misallocation.md.
--
-- QUESTION: do the 8B's event scores predict the RESOLVED ticker's forward
-- price move, and does resolved-ticker attribution (matched vs reattributed)
-- carry tradeable signal that FinBERT's feed-tag attribution cannot?
--
-- RUN:   sqlite3 db/trading.db ".read scripts/validate_news_scores.sql"
--   (cd to project root first; from elsewhere use an absolute path to the db.)
--
-- The forward horizon N is the single literal "+ 5" in the temp-table build
-- below.  Run once at N=5 (one trading week), then edit to "+ 21" (~one month)
-- and re-run.
--
-- SCOPE / CAVEATS (see the finding doc, "Discriminating tests"):
--   * Operates on STORED fields (attributed_symbol, composite_score).  This
--     cleanly separates matched vs reattributed (both have non-null
--     attributed_symbol) but CANNOT separate untracked from digest (both store
--     attributed_symbol = NULL), so it drops them together via the
--     "attributed_symbol IS NOT NULL" filter.  Acceptable: neither is
--     universe-tradeable.
--   * Event score = MEAN of member composite_scores grouped by
--     (attributed_symbol, calendar day) -- matches data/news_dedup.py.
--   * Forward return is RAW price return of the resolved ticker, entry = first
--     daily bar strictly after the event day, exit = N trading bars later.
--     Events without N forward bars yet (too recent) drop out of the JOIN --
--     that is correct; they are not yet validatable.
--   * For a proper Pearson/Spearman correlation and a faithful 4-way split,
--     promote to a Python script mirroring scripts/analyze_wf_vs_live.py using
--     resolve_attribution_status + cluster_news_events (the read-time path).
--     This SQL is the cheap, accumulation-friendly weekly check.
-- ============================================================================

DROP TABLE IF EXISTS _news_fwd;

-- Materialize one row per validatable event with its N-bar forward return.
-- Both result queries below read this temp table (CTE scope does not survive a
-- statement boundary, so a temp table is the portable way to share it).
CREATE TEMP TABLE _news_fwd AS
WITH bars AS (
    -- Per-symbol daily bar sequence, so "N trading bars later" is rn + N.
    SELECT symbol,
           timestamp,
           close,
           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp) AS rn
    FROM ohlcv_bars
    WHERE interval = '1d'
),
events AS (
    -- Cluster scored articles into events: (resolved ticker, calendar day),
    -- event score = mean composite_score.  Drops untracked + digest (NULL).
    SELECT attributed_symbol                              AS ticker,
           DATE(published_at)                             AS event_day,
           CASE WHEN attributed_symbol = symbol
                THEN 'matched' ELSE 'reattributed' END    AS attr_bucket,
           AVG(composite_score)                           AS event_score,
           COUNT(*)                                       AS event_size
    FROM llm_news_analysis
    WHERE attributed_symbol IS NOT NULL      -- excludes untracked + digest
      AND composite_score   IS NOT NULL
      AND parse_ok = 1
    GROUP BY attributed_symbol, DATE(published_at),
             CASE WHEN attributed_symbol = symbol THEN 'matched' ELSE 'reattributed' END
),
entry_bar AS (
    -- Entry bar = first daily bar strictly AFTER the event day (no same-day
    -- lookahead: the score was produced overnight on the event's news).
    SELECT e.ticker, e.event_day, e.attr_bucket, e.event_score, e.event_size,
           (SELECT b.rn FROM bars b
             WHERE b.symbol = e.ticker AND b.timestamp > e.event_day
             ORDER BY b.timestamp LIMIT 1)                AS entry_rn
    FROM events e
)
-- Attach entry close (rn) and exit close (rn + N).  Recent events without an
-- N-bar forward window drop out of the JOIN here.
SELECT eb.ticker, eb.event_day, eb.attr_bucket, eb.event_score, eb.event_size,
       be.close                          AS entry_close,
       bx.close                          AS exit_close,
       (bx.close - be.close) / be.close  AS fwd_ret
FROM entry_bar eb
JOIN bars be ON be.symbol = eb.ticker AND be.rn = eb.entry_rn
JOIN bars bx ON bx.symbol = eb.ticker AND bx.rn = eb.entry_rn + 5;   -- <== N here

-- ---- Result 1: attribution bucket x score sign -----------------------------
-- The H1 discriminator.  Compare directional_hit_rate for matched vs
-- reattributed: if reattributed >> 0.50 too, resolved-ticker attribution adds
-- signal FinBERT's feed-tag attribution cannot (H1).  If ALL buckets ~0.50,
-- the scores are noise (H2).
SELECT
    attr_bucket,
    CASE WHEN event_score >  0.15 THEN 'bull'
         WHEN event_score < -0.15 THEN 'bear'
         ELSE 'neutral' END                               AS score_bucket,
    COUNT(*)                                              AS n_events,
    ROUND(AVG(event_score), 3)                            AS avg_score,
    ROUND(AVG(fwd_ret) * 100, 3)                          AS avg_fwd_ret_pct,
    ROUND(AVG(CASE WHEN (event_score > 0 AND fwd_ret > 0)
                     OR (event_score < 0 AND fwd_ret < 0)
                   THEN 1.0 ELSE 0.0 END), 3)             AS directional_hit_rate
FROM _news_fwd
GROUP BY attr_bucket, score_bucket
ORDER BY attr_bucket, score_bucket;

-- ---- Result 2: headline per-bucket summary (directional events only) --------
-- Neutral (|score| <= 0.15) events excluded -- a near-zero score makes no
-- directional claim, so its "hit rate" is a coin flip that dilutes the signal.
-- This is the single number to track week over week per bucket.
SELECT
    attr_bucket,
    COUNT(*)                                              AS n_directional,
    ROUND(AVG(event_score), 3)                            AS avg_score,
    ROUND(AVG(fwd_ret) * 100, 3)                          AS avg_fwd_ret_pct,
    ROUND(AVG(CASE WHEN (event_score > 0 AND fwd_ret > 0)
                     OR (event_score < 0 AND fwd_ret < 0)
                   THEN 1.0 ELSE 0.0 END), 3)             AS directional_hit_rate
FROM _news_fwd
WHERE ABS(event_score) > 0.15
GROUP BY attr_bucket
ORDER BY attr_bucket;

DROP TABLE _news_fwd;
