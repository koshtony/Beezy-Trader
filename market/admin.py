from django.contrib import admin

from .models import Fundamentals, JobLog, NewsArticle, PriceBar, Projection, RecommendationRun, Stock, StockAnalytics


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ("ticker", "name", "sector", "kind", "is_suspended", "history_backfilled_at")
    list_filter = ("sector", "kind", "is_suspended")
    search_fields = ("ticker", "name")


@admin.register(PriceBar)
class PriceBarAdmin(admin.ModelAdmin):
    list_display = ("stock", "date", "close", "volume", "change", "source")
    list_filter = ("source",)
    search_fields = ("stock__ticker",)
    date_hierarchy = "date"
    list_select_related = ("stock",)


@admin.register(Fundamentals)
class FundamentalsAdmin(admin.ModelAdmin):
    list_display = ("stock", "eps_ttm", "book_value_per_share", "dividend_per_share", "src_roe", "fiscal_period", "fetched_at")
    search_fields = ("stock__ticker",)
    list_select_related = ("stock",)


@admin.register(NewsArticle)
class NewsAdmin(admin.ModelAdmin):
    list_display = ("stock", "label", "sentiment", "title", "source", "published_at")
    list_filter = ("label",)
    search_fields = ("title", "stock__ticker")
    list_select_related = ("stock",)


@admin.register(Projection)
class ProjectionAdmin(admin.ModelAdmin):
    list_display = ("stock", "horizon", "as_of", "base_price", "expected_price", "low", "high", "prob_up")
    list_filter = ("horizon", "as_of")
    search_fields = ("stock__ticker",)
    list_select_related = ("stock",)


@admin.register(StockAnalytics)
class AnalyticsAdmin(admin.ModelAdmin):
    list_display = ("stock", "as_of", "last_price", "composite", "risk", "expected_return_1y", "is_liquid")
    search_fields = ("stock__ticker",)
    list_select_related = ("stock",)


admin.site.register(RecommendationRun)
admin.site.register(JobLog)
