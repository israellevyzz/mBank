from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from account.admin_panel import (
    AdminApprovalDecisionView,
    AdminApprovalsView,
    AdminClientCreateAPIView,
    AdminClientUpdateAPIView,
    AdminClientsView,
    AdminLoginView,
    AdminOverviewView,
    AdminTransactionsView,
)
from account.views import HomePage, LandingPageView, MBankLoginView, RegisterView

urlpatterns = [
    path('', HomePage.as_view(), name='home'),
    path('inicio/', LandingPageView.as_view(), name='landing'),
    path('admin/', admin.site.urls),
    path('bank/', include('account.urls')),

    # Custom Admin Master Panel (Figma)
    path('painel-admin/login/', AdminLoginView.as_view(), name='admin_login'),
    path('painel-admin/', AdminOverviewView.as_view(), name='admin_overview'),
    path('painel-admin/clientes/', AdminClientsView.as_view(), name='admin_clients'),
    path('painel-admin/aprovacoes/', AdminApprovalsView.as_view(), name='admin_approvals'),
    path('painel-admin/transacoes/', AdminTransactionsView.as_view(), name='admin_transactions'),
    path('painel-admin/api/clientes/', AdminClientCreateAPIView.as_view(), name='admin_api_client_create'),
    path('painel-admin/api/clientes/<int:pk>/atualizar/', AdminClientUpdateAPIView.as_view(), name='admin_api_client_update'),
    path('painel-admin/api/aprovacoes/<int:pk>/decidir/', AdminApprovalDecisionView.as_view(), name='admin_api_approval_decision'),

    # Auth routes
    path('login/', MBankLoginView.as_view(), name='login'),
    path('cadastro/', RegisterView.as_view(), name='register'),
    path('logout/', auth_views.LogoutView.as_view(next_page='home'), name='logout'),
]

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [
        path('__debug__/', include(debug_toolbar.urls)),
    ] + urlpatterns
