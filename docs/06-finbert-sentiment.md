# FinBERT & Sentiment Analysis

## Why sentiment matters in trading

Price and volume data capture what the market has already done. News captures what is happening right now — earnings surprises, regulatory actions, product launches, management changes — events that move prices before they appear in the technical indicators. A model that ignores news is blind to a major driver of short-term price movement.

The challenge: news is text, and text isn't directly usable by a numerical model. We need a way to convert "Apple beats earnings by 15%" into a number. That's what sentiment analysis does.

---

## From bag-of-words to transformers

### Early NLP: bag-of-words
The simplest approach to text sentiment is counting positive and negative words:
```
"Strong earnings growth" → {strong: +1, earnings: 0, growth: +1} → score: +2
```
This ignores word order, context, and domain-specific meaning. "Not strong" would score +1 instead of -1.

### Word embeddings (Word2Vec, GloVe)
A major improvement: represent each word as a dense vector in a high-dimensional space where similar words are geometrically close. "bull" and "bullish" cluster together; "earnings beat" and "profit surprise" land in similar regions.

Still limited: each word has one fixed representation regardless of context. "Bank" in "river bank" vs "investment bank" maps to the same vector.

### Transformers and BERT
[BERT](https://arxiv.org/abs/1810.04805) (Bidirectional Encoder Representations from Transformers) was introduced by Google in 2018 and changed NLP fundamentally.

BERT produces **contextual embeddings** — the representation of each word depends on all the surrounding words. "Bank" in a financial context gets a different vector than "bank" in a geographic context.

The key mechanism is **self-attention**: for each word, the model computes how much attention to pay to every other word in the sentence. This is learned during pre-training on massive text corpora.

```
Sentence: "Apple beats earnings estimates by wide margin"

Attention pattern for "beats":
  Apple    → 0.35  (subject, highly relevant)
  earnings → 0.40  (direct object, core meaning)
  estimates→ 0.15  (context)
  margin   → 0.10  (modifier)
```

---

## FinBERT

[FinBERT](https://huggingface.co/ProsusAI/finbert) is BERT fine-tuned specifically on financial text — earnings reports, analyst notes, news articles — using labeled sentiment data. This domain adaptation is critical because financial language has different patterns than general text:

| General text | Financial text |
|-------------|---------------|
| "Missed" → negative | "Missed estimates" → negative (but context matters — by how much?) |
| "Volatile" → neutral/negative | "High volatility" → opportunity or risk depending on context |
| "Flat" → neutral | "Flat guidance" → potentially negative (analyst disappointment) |

Fine-tuning on financial text teaches FinBERT these domain-specific nuances.

### Architecture

```
Input text (headline)
      ↓
Tokenizer (WordPiece)
      ↓
BERT encoder (12 transformer layers)
      ↓
[CLS] token representation   ← captures whole-sentence meaning
      ↓
Classification head (linear layer)
      ↓
Softmax → {positive: p₁, negative: p₂, neutral: p₃}
```

The `[CLS]` token is a special token prepended to every input. After passing through all 12 transformer layers, its representation encodes the aggregate meaning of the entire sentence — and is fed into the classification head.

### Model size

FinBERT has ~110 million parameters and is approximately 400 MB on disk. It loads once per session (not per headline) and is cached in memory. The first invocation is slow (~30 seconds to download if not cached locally); subsequent invocations are fast (milliseconds per headline on CPU, faster on GPU).

---

## How FinBERT is used in this project

### Scoring a headline

```python
from transformers import pipeline

pipe = pipeline(
    "text-classification",
    model="ProsusAI/finbert",
    tokenizer="ProsusAI/finbert",
)

result = pipe("Apple reports record quarterly revenue, shares surge")
# → [{"label": "positive", "score": 0.97}]

# Convert to [-1, 1]
if result[0]["label"] == "positive":
    score = result[0]["score"]        # +0.97
elif result[0]["label"] == "negative":
    score = -result[0]["score"]       # negative
else:
    score = 0.0                       # neutral
```

### IBKR headline cleaning

IBKR headlines arrive with a Dow Jones prefix that confuses FinBERT:
```
"{A:800015:L:en} Apple Reports Record Q4 Earnings"
```
The prefix `{A:800015:L:en}` is stripped before scoring. Passing it to FinBERT would waste tokens and potentially degrade classification.

### Time-decay aggregation

A single headline is rarely the whole story. FinBERT aggregates all recent headlines for a symbol using exponential time decay:

```
sentiment_score = Σ(score_i × weight_i) / Σ(weight_i)

where weight_i = exp(-ln(2) × age_hours_i / half_life_hours)
```

With `half_life_hours=24` (default):
- Article from 1 hour ago: weight = 0.97
- Article from 24 hours ago: weight = 0.50
- Article from 48 hours ago: weight = 0.25
- Article from 7 days (168 hours) ago: weight ≈ 0.001 → effectively ignored

Articles older than `sentiment_staleness_days` (default 7) contribute zero weight and are excluded entirely.

### Effect of news volume

A symbol with 20 recent articles has a much more robust sentiment signal than one with 2 articles. The aggregation naturally handles this — more articles = more evidence = stronger signal (or cancellation of conflicting signals).

If no news is available for a symbol, `FinBERT.predict()` returns `0.0` (neutral) rather than raising an error.

---

## Lookahead prevention

During walk-forward backtesting, FinBERT is called with `as_of=bar_timestamp`:

```python
finbert_score = finbert.predict(df, symbol, as_of=bar_date)
```

This filters the news cache to only include articles with `published_at <= as_of`. For historical bars, this means FinBERT only uses news that existed on that date — it cannot "see" articles published later.

Without this constraint, a historical bar from 6 months ago would use today's news to generate a signal, which is a severe form of lookahead bias — the future news would almost certainly make the signal more accurate than it would have been in reality.

The `as_of` parameter also skips the live API fetch for historical bars. There's no point fetching new articles from Alpaca for a bar that happened 6 months ago — the cached articles (filtered by date) are what matters.

---

## FinBERT coverage tracking

Not every bar in the test window has associated news. FinBERT's influence is tracked via **coverage**:

```
finbert_coverage = bars_with_nonzero_score / total_test_bars
```

After each walk-forward fold, FinBERT's weight is scaled by its coverage:

```python
finbert_weight = configured_base_weight × finbert_coverage
```

A fold where news only exists for 30% of test bars gives FinBERT 30% of its normal weight. This prevents the model from relying on sentiment in periods where news data is sparse.

Coverage is tracked and stored in `walk_forward_results.sentiment_note` for each fold. You can see it on the Walk-Forward page (Page 4) in the results table.

---

## Limitations

**FinBERT evaluate() is a stub.** FinBERT returns `{"sharpe_ratio": 0.0}` from its `evaluate()` method because sentiment-based signals can't be backtested the same way price models can — you can't calculate a Sharpe ratio purely from FinBERT outputs without making assumptions about execution. This is why FinBERT is excluded from the LSTM vs XGBoost Sharpe competition in the ensemble rebalancer.

**Quality depends on news volume.** For large-cap, heavily covered stocks (AAPL, MSFT, AMZN), FinBERT has abundant material. For mid-cap stocks in the dynamic universe, news may be sparse. The coverage-based weight scaling handles this gracefully.

**Headline ≠ article.** FinBERT only sees the headline, not the full article body. "Company Announces Restructuring" could be bullish (efficiency) or bearish (distress) depending on context that only appears in the article body. IBKR's Dow Jones headlines tend to be more informative than yfinance's short summaries.
