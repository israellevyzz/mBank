from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import login as auth_login
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction as db_transaction
from django.db.models import Q
from django.forms import ModelChoiceField
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, TemplateView
from rest_framework import mixins, status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .api_support import (
    generate_unique_iban,
    normalize_cpf_username,
    serialize_transaction_for_user,
)
from .forms import TransactionForm
from .models import Account, Client, PiggyBank, Transaction
from .serializers import AccountSerializer, TransactionSerializer
from .templatetags.mbank_tags import PT_MONTHS


def format_relative_pt(dt):
    local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    today = timezone.localdate()
    d = local_dt.date()
    time_str = local_dt.strftime('%H:%M')
    if d == today:
        return f'Hoje, {time_str}'
    if d == today - timedelta(days=1):
        return f'Ontem, {time_str}'
    return f'{local_dt.strftime("%d/%m/%Y")}, {time_str}'


def format_group_header_pt(d):
    today = timezone.localdate()
    month_name = PT_MONTHS[d.month].upper()
    if d == today:
        return f'HOJE, {d.day} DE {month_name}'
    if d == today - timedelta(days=1):
        return f'ONTEM, {d.day} DE {month_name}'
    return f'{d.day} DE {month_name}'


class CPFAuthenticationForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        'invalid_login': 'CPF/usuário ou senha incorretos. Confira seus dados e tente novamente.',
        'inactive': 'Esta conta está inativa. Entre em contato com o suporte mBank.',
    }

    def clean_username(self):
        raw = self.cleaned_data.get('username', '')
        normalized = normalize_cpf_username(raw)
        if normalized != raw and Client.objects.filter(username=normalized).exists():
            return normalized
        return raw

    def clean(self):
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')
        if username and password:
            candidate = Client.objects.filter(username=username).first()
            if candidate and not candidate.is_active and candidate.check_password(password):
                if candidate.approval_status == 'PENDING':
                    raise DjangoValidationError(
                        'Seu cadastro ainda está em análise pelo Admin Master. Aguarde a aprovação para acessar sua conta.'
                    )
                if candidate.approval_status == 'REJECTED':
                    raise DjangoValidationError(
                        'Sua solicitação de abertura de conta foi recusada. Entre em contato com o suporte mBank.'
                    )
                raise DjangoValidationError(self.error_messages['inactive'])
        return super().clean()


class MBankLoginView(auth_views.LoginView):
    template_name = 'registration/login.html'
    authentication_form = CPFAuthenticationForm
    redirect_authenticated_user = True

    def get_success_url(self):
        redirect_to = self.get_redirect_url()
        if redirect_to:
            return redirect_to
        if self.request.user.is_staff:
            return str(reverse_lazy('admin_overview'))
        return super().get_success_url()


class RegisterView(View):
    template_name = 'registration/register.html'

    def get(self, request):
        if request.user.is_authenticated:
            return redirect('home')
        return render(request, self.template_name, {'values': {}, 'errors': []})

    def post(self, request):
        if request.user.is_authenticated:
            return redirect('home')
        name = request.POST.get('name', '').strip()
        cpf = request.POST.get('cpf', '').strip()
        birth_date = request.POST.get('birth_date', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')

        values = {
            'name': name,
            'cpf': cpf,
            'birth_date': birth_date,
            'phone_number': phone_number,
            'email': email,
        }
        errors = []
        username = normalize_cpf_username(cpf)
        if not name:
            errors.append('Informe seu nome completo.')
        if not username:
            errors.append('Informe um CPF ou nome de usuário válido.')
        elif Client.objects.filter(username=username).exists():
            errors.append('Já existe uma conta cadastrada com este CPF/usuário.')
        if not email:
            errors.append('Informe um e-mail válido.')
        if not password:
            errors.append('Crie uma senha para sua conta.')
        elif password != password_confirm:
            errors.append('As senhas informadas não coincidem.')
        else:
            try:
                validate_password(password)
            except DjangoValidationError as exc:
                errors.extend(exc.messages)

        if errors:
            return render(request, self.template_name, {'values': values, 'errors': errors}, status=400)

        with db_transaction.atomic():
            client = Client.objects.create_user(
                username=username,
                email=email,
                password=password,
                name=name,
                phone_number=phone_number,
                birth_date=birth_date,
                approval_status='PENDING',
                is_active=False,
            )

        return render(request, self.template_name, {
            'values': {},
            'errors': [],
            'pending_submitted': True,
            'submitted_client': {
                'name': client.name,
                'cpf': cpf,
                'email': client.email,
            },
        })


class LandingPageView(TemplateView):
    template_name = 'landing.html'


class HomePage(TemplateView):
    template_name = 'homepage.html'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return render(request, 'landing.html')
        if request.user.is_staff:
            return redirect('admin_overview')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        open_accounts = list(Account.objects.filter(owner=user, closed=None).order_by('pk'))
        account_ids = {a.pk for a in open_accounts}
        all_user_account_ids = set(Account.objects.filter(owner=user).values_list('pk', flat=True))

        total_balance = sum((a.balance for a in open_accounts), Decimal('0.00'))
        total_available = sum((a.available_balance for a in open_accounts), Decimal('0.00'))
        total_reserved = sum((a.reserved_balance for a in open_accounts), Decimal('0.00'))

        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_txs = list(
            Transaction.objects.filter(
                Q(src__owner=user) | Q(dest__owner=user),
                timestamp__gte=month_start,
            ).select_related('src__owner', 'dest__owner')
        )

        inflows_total = Decimal('0.00')
        inflows_count = 0
        outflows_total = Decimal('0.00')
        outflows_count = 0

        for tx in month_txs:
            if tx.dest_id in all_user_account_ids and tx.src_id not in all_user_account_ids:
                inflows_total += tx.amount
                inflows_count += 1
            elif tx.src_id in all_user_account_ids and tx.dest_id not in all_user_account_ids:
                outflows_total += tx.amount
                outflows_count += 1

        recent_qs = (
            Transaction.objects.filter(Q(src__owner=user) | Q(dest__owner=user))
            .select_related('src__owner', 'dest__owner')
            .order_by('-timestamp', '-pk')[:5]
        )
        recent_items = []
        for tx in recent_qs:
            item = serialize_transaction_for_user(tx, user)
            acct_iban = item['dest_iban'] if item['direction'] == 'in' else (item['src_iban'] or item['dest_iban'])
            item['meta_line'] = f"{format_relative_pt(tx.timestamp)} · {acct_iban or ''}".strip(' ·')
            recent_items.append(item)

        context.update({
            'active_nav': 'home',
            'page_kicker': 'Visão geral',
            'open_accounts': open_accounts,
            'open_accounts_count': len(open_accounts),
            'total_balance': total_balance,
            'total_available': total_available,
            'total_reserved': total_reserved,
            'inflows_total': inflows_total,
            'inflows_count': inflows_count,
            'outflows_total': outflows_total,
            'outflows_count': outflows_count,
            'recent_transactions': recent_items,
            'has_accounts': bool(account_ids),
        })
        return context


class ClientListView(LoginRequiredMixin, ListView):
    model = Client

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update({'active_nav': 'accounts', 'page_kicker': 'Clientes'})
        return ctx


class ClientDetailView(LoginRequiredMixin, DetailView):
    model = Client

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update({'active_nav': 'accounts', 'page_kicker': 'Detalhes do cliente'})
        return ctx


class AccountListView(LoginRequiredMixin, ListView):
    model = Account

    def get_queryset(self):
        return Account.objects.filter(owner=self.request.user).order_by('closed', 'pk')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        accounts = list(self.object_list)
        open_accounts = [a for a in accounts if a.is_open()]
        total_balance = sum((a.balance for a in open_accounts), Decimal('0.00'))
        total_available = sum((a.available_balance for a in open_accounts), Decimal('0.00'))
        total_reserved = sum((a.reserved_balance for a in open_accounts), Decimal('0.00'))

        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        monthly_ops = Transaction.objects.filter(
            Q(src__owner=user) | Q(dest__owner=user),
            timestamp__gte=month_start,
        ).count()

        cards = []
        for idx, acc in enumerate(accounts):
            ops_count = Transaction.objects.filter(Q(src=acc) | Q(dest=acc)).count()
            if idx == 0:
                title, kind_label = 'Conta Principal', 'Corrente'
            elif idx == 1:
                title, kind_label = 'Conta Secundária / Reserva', 'Poupança / Reserva'
            else:
                title, kind_label = f'Conta #{idx + 1}', 'Corrente'
            cards.append({
                'account': acc,
                'title': title,
                'kind_label': kind_label,
                'ops_count': ops_count,
                'is_open': acc.is_open(),
            })

        context.update({
            'active_nav': 'accounts',
            'page_kicker': 'Minhas contas',
            'total_balance': total_balance,
            'total_available': total_available,
            'total_reserved': total_reserved,
            'active_accounts_count': len(open_accounts),
            'monthly_ops_count': monthly_ops,
            'account_cards': cards,
        })
        return context


class TransactionListView(LoginRequiredMixin, ListView):
    model = Transaction
    ordering = ['-timestamp', '-pk']

    def get_selected_account(self):
        if 'account_id' in self.kwargs:
            return get_object_or_404(Account, pk=self.kwargs['account_id'], owner=self.request.user)
        q_acc = self.request.GET.get('account_id')
        if q_acc:
            try:
                return get_object_or_404(Account, pk=int(q_acc), owner=self.request.user)
            except ValueError:
                return None
        return None

    def get_queryset(self):
        ordering = self.get_ordering()
        selected = self.get_selected_account()
        if selected is not None:
            qs = Transaction.objects.filter(Q(src__id=selected.pk) | Q(dest__id=selected.pk))
        else:
            client = self.request.user
            qs = Transaction.objects.filter(Q(src__owner=client) | Q(dest__owner=client))

        days_param = self.request.GET.get('days', '').strip()
        if days_param.isdigit() and int(days_param) > 0:
            qs = qs.filter(timestamp__gte=timezone.now() - timedelta(days=int(days_param)))

        return qs.select_related('src__owner', 'dest__owner').order_by(*ordering)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        selected = self.get_selected_account()
        open_accounts = list(Account.objects.filter(owner=user, closed=None).order_by('pk'))
        if selected is not None:
            available_balance = selected.available_balance
        else:
            available_balance = sum((a.available_balance for a in open_accounts), Decimal('0.00'))

        days_param = self.request.GET.get('days', '').strip()
        active_days = int(days_param) if days_param.isdigit() else 0

        groups = OrderedDict()
        for tx in self.object_list:
            local_dt = timezone.localtime(tx.timestamp) if timezone.is_aware(tx.timestamp) else tx.timestamp
            header = format_group_header_pt(local_dt.date())
            item = serialize_transaction_for_user(tx, user, selected.pk if selected else None)
            item['time_str'] = local_dt.strftime('%H:%M')
            item['date_full'] = local_dt.strftime('%d/%m/%Y %H:%M')
            groups.setdefault(header, []).append(item)

        context.update({
            'active_nav': 'statement',
            'page_kicker': 'Extrato',
            'selected_account': selected,
            'open_accounts': open_accounts,
            'available_balance': available_balance,
            'active_days': active_days,
            'grouped_transactions': list(groups.items()),
        })
        return context


class AccountCreate(LoginRequiredMixin, CreateView):
    model = Account
    fields = ['iban']
    success_url = reverse_lazy('bank:accounts_list')

    def get_initial(self):
        return {'iban': generate_unique_iban()}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update({'active_nav': 'accounts', 'page_kicker': 'Minhas contas'})
        return ctx

    def form_valid(self, form):
        form.instance.owner = self.request.user
        return super().form_valid(form)


class TransactionCreate(LoginRequiredMixin, CreateView):
    model = Transaction
    success_url = reverse_lazy('bank:transactions_list')
    form_class = TransactionForm

    def get_initial(self):
        return {
            'src': ModelChoiceField(queryset=Account.objects.filter(owner=self.request.user, closed=None),
                                    empty_label='no account', required=False),
            'dest': ModelChoiceField(queryset=Account.objects.filter(closed=None), empty_label='no account',
                                     required=False),
        }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update({'active_nav': 'transfer', 'page_kicker': 'Nova transação'})
        return ctx

    def form_valid(self, form):
        transaction = form.instance
        src = transaction.src
        dest = transaction.dest
        amount = transaction.amount

        request = {'actor': self.request.user, 'key': form.cleaned_data['request_key']}
        try:
            if src and dest:
                src.transfer(dest, amount, **request)
            elif dest:
                dest.deposit(amount, **request)
            elif src:
                src.withdraw(amount, **request)
            else:
                form.add_error(None, 'Choose a source and/or a destination account.')
                return self.form_invalid(form)
        except ValueError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)

        return HttpResponseRedirect(self.success_url)


class DepositPageView(LoginRequiredMixin, TemplateView):
    template_name = 'account/deposit.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        open_accounts = list(Account.objects.filter(owner=user, closed=None).order_by('pk'))
        acc_param = self.request.GET.get('account_id')
        selected = None
        if acc_param and acc_param.isdigit():
            selected = next((a for a in open_accounts if a.pk == int(acc_param)), None)
        if selected is None and open_accounts:
            selected = open_accounts[0]

        recent_deposits_qs = (
            Transaction.objects.filter(dest__owner=user, src__isnull=True)
            .exclude(transaction_type='PIGGY_YIELD')
            .select_related('dest')
            .order_by('-timestamp', '-pk')[:6]
        )
        recent_deposits = [
            {
                'id': tx.pk,
                'amount': tx.amount,
                'iban': tx.dest.iban if tx.dest else '',
                'when': format_relative_pt(tx.timestamp),
            }
            for tx in recent_deposits_qs
        ]
        context.update({
            'active_nav': 'deposit',
            'page_kicker': 'Depósito',
            'open_accounts': open_accounts,
            'selected_account': selected,
            'recent_deposits': recent_deposits,
        })
        return context


class WithdrawPageView(LoginRequiredMixin, TemplateView):
    template_name = 'account/withdraw.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        open_accounts = list(Account.objects.filter(owner=user, closed=None).order_by('pk'))
        acc_param = self.request.GET.get('account_id')
        selected = None
        if acc_param and acc_param.isdigit():
            selected = next((a for a in open_accounts if a.pk == int(acc_param)), None)
        if selected is None and open_accounts:
            selected = open_accounts[0]
        context.update({
            'active_nav': 'withdraw',
            'page_kicker': 'Saque',
            'open_accounts': open_accounts,
            'selected_account': selected,
        })
        return context


class TransferPageView(LoginRequiredMixin, TemplateView):
    template_name = 'account/transfer.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        open_accounts = list(Account.objects.filter(owner=user, closed=None).order_by('pk'))
        acc_param = self.request.GET.get('account_id')
        selected = None
        if acc_param and acc_param.isdigit():
            selected = next((a for a in open_accounts if a.pk == int(acc_param)), None)
        if selected is None and open_accounts:
            selected = open_accounts[0]
        context.update({
            'active_nav': 'transfer',
            'page_kicker': 'Transferência',
            'open_accounts': open_accounts,
            'selected_account': selected,
        })
        return context


class PiggyBankPageView(LoginRequiredMixin, TemplateView):
    template_name = 'account/piggy_bank.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        open_accounts = list(Account.objects.filter(owner=user, closed=None).order_by('pk'))
        has_active_piggies = PiggyBank.objects.filter(
            account__owner=user,
            archived_at__isnull=True,
        ).exists()
        context.update({
            'active_nav': 'piggy',
            'page_kicker': '',
            'open_accounts': open_accounts,
            'default_account': open_accounts[0] if open_accounts else None,
            'has_active_piggies': has_active_piggies,
        })
        return context


class LoanPageView(LoginRequiredMixin, TemplateView):
    template_name = 'account/loan.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        open_accounts = list(Account.objects.filter(owner=user, closed=None).order_by('pk'))
        context.update({
            'active_nav': 'loan',
            'page_kicker': 'Empréstimo',
            'open_accounts': open_accounts,
            'default_account': open_accounts[0] if open_accounts else None,
        })
        return context


class TransactionViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    queryset = Transaction.objects.all()

    def list(self, request, format='json'):
        q1 = Transaction.objects.filter(src__owner=self.request.user)
        q2 = Transaction.objects.filter(dest__owner=self.request.user)
        serializer = TransactionSerializer(q1 | q2, many=True)
        return Response(serializer.data)

    def retrieve(self, request, pk=None):
        queryset = get_object_or_404(Transaction, Q(src__owner=request.user) | Q(dest__owner=request.user), pk=pk)
        serializer = TransactionSerializer(queryset)
        return Response(serializer.data)


class AccountViewSet(mixins.CreateModelMixin,
                     mixins.ListModelMixin,
                     viewsets.GenericViewSet):
    serializer_class = AccountSerializer
    queryset = Account.objects.all()

    def list(self, request):
        serializer = AccountSerializer(self.get_queryset(), many=True)
        return Response(serializer.data)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(owner=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def get_queryset(self):
        return Account.objects.filter(owner=self.request.user)
