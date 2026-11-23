from django.db import models


class Stock(models.Model):
    """A security listed on the Nairobi Securities Exchange (or an index such as NASI)."""

    KIND_EQUITY = "equity"
    KIND_INDEX = "index"
    KIND_CHOICES = [(KIND_EQUITY, "Equity"), (KIND_INDEX, "Index")]

    ticker = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=KIND_EQUITY)
    sector = models.CharField(max_length=80, blank=True)
    industry = models.CharField(max_length=120, blank=True)
    is_suspended = models.BooleanField(default=False)
    news_query = models.CharField(
        max_length=200, blank=True, help_text="Override for the Google News search phrase."
    )
    history_backfilled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ticker"]

    def __str__(self):
        return f"{self.ticker} — {self.name}"

    @property
    def is_bank(self) -> bool:
        s = f"{self.sector} {self.industry}".lower()
        return "bank" in s

    @property
    def is_financial(self) -> bool:
        s = f"{self.sector} {self.industry}".lower()
        return any(k in s for k in ("bank", "insurance", "investment", "financ"))


class PriceBar(models.Model):
    """One end-of-day close for a stock."""

    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="prices")
    date = models.DateField()
    close = models.FloatField()
    volume = models.BigIntegerField(null=True, blank=True)
    change = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=16, default="afx")

    class Meta:
        ordering = ["stock", "date"]
        constraints = [models.UniqueConstraint(fields=["stock", "date"], name="uniq_stock_date")]
        indexes = [models.Index(fields=["stock", "-date"])]

    def __str__(self):
        return f"{self.stock.ticker} {self.date} {self.close}"


class Fundamentals(models.Model):
    """Latest fundamental inputs for a stock (TTM where available)."""

    stock = models.OneToOneField(Stock, on_delete=models.CASCADE, related_name="fundamentals")
    # raw inputs
    eps_ttm = models.FloatField(null=True, blank=True)
    eps_history = models.JSONField(default=list, blank=True)  # [{"period": "FY 2025", "eps": 1.7}, ...]
    revenue_ttm = models.FloatField(null=True, blank=True)
    gross_profit_ttm = models.FloatField(null=True, blank=True)
    net_income_ttm = models.FloatField(null=True, blank=True)
    equity = models.FloatField(null=True, blank=True)
    book_value_per_share = models.FloatField(null=True, blank=True)
    total_debt = models.FloatField(null=True, blank=True)
    cash = models.FloatField(null=True, blank=True)
    operating_cash_flow = models.FloatField(null=True, blank=True)
    capex = models.FloatField(null=True, blank=True)
    free_cash_flow = models.FloatField(null=True, blank=True)
    dividend_per_share = models.FloatField(null=True, blank=True)
    shares_outstanding = models.FloatField(null=True, blank=True)
    # source-reported ratios (used as fallbacks / cross-checks)
    src_pe = models.FloatField(null=True, blank=True)
    src_forward_pe = models.FloatField(null=True, blank=True)
    src_peg = models.FloatField(null=True, blank=True)
    src_pb = models.FloatField(null=True, blank=True)
    src_ps = models.FloatField(null=True, blank=True)
    src_roe = models.FloatField(null=True, blank=True)
    src_roa = models.FloatField(null=True, blank=True)
    src_debt_to_equity = models.FloatField(null=True, blank=True)
    src_gross_margin = models.FloatField(null=True, blank=True)
    src_profit_margin = models.FloatField(null=True, blank=True)
    src_payout_ratio = models.FloatField(null=True, blank=True)
    src_current_ratio = models.FloatField(null=True, blank=True)
    eps_growth_forecast = models.FloatField(null=True, blank=True)  # % p.a. (3Y)
    revenue_growth_forecast = models.FloatField(null=True, blank=True)
    analyst_target = models.FloatField(null=True, blank=True)
    analyst_consensus = models.CharField(max_length=40, blank=True)
    analyst_count = models.IntegerField(null=True, blank=True)
    fiscal_period = models.CharField(max_length=40, blank=True)
    source = models.CharField(max_length=40, default="stockanalysis")
    fetched_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Fundamentals({self.stock.ticker})"


class NewsArticle(models.Model):
    POSITIVE, NEGATIVE, NEUTRAL = "positive", "negative", "neutral"
    LABELS = [(POSITIVE, "Positive"), (NEGATIVE, "Negative"), (NEUTRAL, "Neutral")]

    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="news")
    title = models.CharField(max_length=500)
    url = models.URLField(max_length=1000)
    source = models.CharField(max_length=200, blank=True)
    published_at = models.DateTimeField(db_index=True)
    sentiment = models.FloatField(default=0.0)
    label = models.CharField(max_length=8, choices=LABELS, default=NEUTRAL)
    matched_terms = models.JSONField(default=list, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-published_at"]
        constraints = [models.UniqueConstraint(fields=["stock", "title"], name="uniq_stock_headline")]

    def __str__(self):
        return f"[{self.label}] {self.title}"


class Projection(models.Model):
    HORIZONS = [("1D", "Daily"), ("1W", "Weekly"), ("1M", "Monthly"), ("1Y", "Yearly")]

    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="projections")
    horizon = models.CharField(max_length=2, choices=HORIZONS)
    as_of = models.DateField()
    target_date = models.DateField()
    base_price = models.FloatField()
    expected_price = models.FloatField()
    low = models.FloatField()
    high = models.FloatField()
    expected_return = models.FloatField()  # fraction, e.g. 0.05 = +5%
    prob_up = models.FloatField()
    components = models.JSONField(default=dict, blank=True)
    backtest = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["stock", "as_of", "horizon"]
        constraints = [
            models.UniqueConstraint(fields=["stock", "horizon", "as_of"], name="uniq_projection")
        ]


class StockAnalytics(models.Model):
    """Computed snapshot: metrics, risk and scores. Rebuilt by the analysis pipeline."""

    stock = models.OneToOneField(Stock, on_delete=models.CASCADE, related_name="analytics")
    as_of = models.DateField(null=True)
    last_price = models.FloatField(null=True)
    change = models.FloatField(null=True)
    change_pct = models.FloatField(null=True)
    volume = models.BigIntegerField(null=True)
    metrics = models.JSONField(default=dict)  # valuation / profitability / debt / dividend
    technicals = models.JSONField(default=dict)  # returns, volatility, RSI, drawdown...
    scores = models.JSONField(default=dict)  # pillar scores 0..100
    composite = models.FloatField(null=True, db_index=True)
    risk = models.FloatField(null=True)
    expected_return_1y = models.FloatField(null=True)
    expected_return_1m = models.FloatField(null=True)
    sentiment = models.FloatField(null=True)
    is_liquid = models.BooleanField(default=False)
    reasons = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    computed_at = models.DateTimeField(auto_now=True)


class RecommendationRun(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    as_of = models.DateField(null=True)
    payload = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]


class JobLog(models.Model):
    RUNNING, OK, FAILED = "running", "ok", "failed"

    job = models.CharField(max_length=40, db_index=True)
    status = models.CharField(max_length=10, default=RUNNING)
    message = models.TextField(blank=True)
    progress = models.CharField(max_length=120, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.job} {self.status} @ {self.started_at:%Y-%m-%d %H:%M}"


class MacroPoint(models.Model):
    """One observation of a macro series (CBK rates, inflation, USD/KES, Brent...)."""

    series = models.CharField(max_length=20, db_index=True)
    date = models.DateField()
    value = models.FloatField()
    source = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["series", "date"]
        constraints = [models.UniqueConstraint(fields=["series", "date"], name="uniq_macro_point")]

    def __str__(self):
        return f"{self.series} {self.date} {self.value}"
