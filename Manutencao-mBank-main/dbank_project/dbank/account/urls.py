from django.urls import path, re_path
from rest_framework.routers import DefaultRouter

from .api_support import (
    AccountCloseView,
    AccountDepositView,
    AccountTransferView,
    AccountWithdrawView,
    MeView,
    StatementView,
)
from .loans import LoanSimulationViewSet
from .piggy_banks import PiggyBankViewSet
from .purchases import PurchaseViewSet
from .views import (
    AccountCreate,
    AccountListView,
    AccountViewSet,
    ClientDetailView,
    ClientListView,
    DepositPageView,
    LoanPageView,
    PiggyBankPageView,
    TransactionCreate,
    TransactionListView,
    TransactionViewSet,
    TransferPageView,
    WithdrawPageView,
)

app_name = 'bank'

transaction_router = DefaultRouter()
transaction_router.register(r'api/transactions', TransactionViewSet)

account_router = DefaultRouter()
account_router.register(r'api/accounts', AccountViewSet)

account_router.register(r'api/compras', PurchaseViewSet, basename='compra')
account_router.register(r'api/emprestimos', LoanSimulationViewSet, basename='emprestimo')
account_router.register(r'api/cofrinhos', PiggyBankViewSet, basename='cofrinho')

urlpatterns = transaction_router.urls + account_router.urls

urlpatterns += [
    # Supporting REST API endpoints for the frontend
    path('api/me/', MeView.as_view(), name='api_me'),
    path('api/extrato/', StatementView.as_view(), name='api_statement'),
    path('api/accounts/<int:pk>/deposito/', AccountDepositView.as_view(), name='api_account_deposit'),
    path('api/accounts/<int:pk>/saque/', AccountWithdrawView.as_view(), name='api_account_withdraw'),
    path('api/accounts/<int:pk>/transferencia/', AccountTransferView.as_view(), name='api_account_transfer'),
    path('api/accounts/<int:pk>/encerrar/', AccountCloseView.as_view(), name='api_account_close'),

    # Client banking web pages
    path('clients/', ClientListView.as_view(), name='clients_list'),
    re_path(r'^clients/(?P<pk>\d+)$', ClientDetailView.as_view(), name='clients_detail'),
    path('accounts/', AccountListView.as_view(), name='accounts_list'),
    path('accounts/create', AccountCreate.as_view(), name='accounts_create'),
    re_path(r'^accounts/(?P<account_id>\d+)/transactions$', TransactionListView.as_view(), name='account_transactions'),
    path('transactions/', TransactionListView.as_view(), name='transactions_list'),
    path('transactions/create', TransactionCreate.as_view(), name='transactions_create'),
    path('deposito/', DepositPageView.as_view(), name='deposit'),
    path('saque/', WithdrawPageView.as_view(), name='withdraw'),
    path('transferencia/', TransferPageView.as_view(), name='transfer'),
    path('cofrinho/', PiggyBankPageView.as_view(), name='piggy_bank'),
    path('emprestimo/', LoanPageView.as_view(), name='loan'),
]
