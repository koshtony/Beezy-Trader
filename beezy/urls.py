from django.contrib import admin
from django.urls import path

from market import views
from market.api import api

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("screener/", views.screener, name="screener"),
    path("stocks/<str:ticker>/", views.stock, name="stock"),
    path("calculator/", views.calculator, name="calculator"),
    path("experiments/", views.experiments_page, name="experiments"),
    path("data/", views.data_page, name="data"),
    path("news/", views.news_page, name="news"),
    path("method/", views.method, name="method"),
    path("api/", api.urls),
    path("admin/", admin.site.urls),
    path("healthz", views.healthz, name="healthz"),
]
