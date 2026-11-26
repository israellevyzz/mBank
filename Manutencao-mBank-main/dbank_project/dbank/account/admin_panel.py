import json
from decimal import Decimal, InvalidOperation

from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from .api_support import generate_unique_iban, normalize_cpf_username
from .context_processors import compute_initials
from .models import Account, Client, Transaction
from .views import format_relative_pt


def format_doc(username):
    digits = ''.join(ch for ch in (username or '') if ch.isdigit())
    if len(digits) == 11:
        return f'{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}'
    if len(digits) == 14:
        return f'{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}'
    return username


class AdminAuthenticationForm(AuthenticationForm):
    error_messages = {
        'invalid_login': 'Credencial administrativa ou senha incorretas.',
        'inactive': 'Esta conta administrativa está inativa.',
    }

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise DjangoValidationError(
                'Esta conta não possui permissão de administrador. Utilize o acesso de cliente.',
                code='not_staff',
            )


class AdminLoginView(auth_views.LoginView):
    template_name = 'registration/admin_login.html'
    authentication_form = AdminAuthenticationForm

    def get_success_url(self):
        return self.get_redirect_url() or str(reverse_lazy('admin_overview'))


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    login_url = 'admin_login'
    raise_exception = False

    def test_func(self):
        return bool(self.request.user and self.request.user.is_staff)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            return JsonResponse({'detail': 'Acesso restrito a administradores.'}, status=403)
        return super().handle_no_permission()


def _format_global_tx(tx):
    kind = tx.transaction_type or tx.get_type().upper()
    local_dt = timezone.localtime(tx.timestamp) if timezone.is_aware(tx.timestamp) else tx.timestamp

    if kind == 'DEPOSIT' or (tx.src_id is None and kind != 'PIGGY_YIELD'):
        type_label = 'Depósito'
        type_color = 'emerald'
        amount_prefix = '+ '
        origin_name = 'Depósito externo'
        dest_name = (tx.dest.owner.name or tx.dest.owner.username) if tx.dest else '—'
        actor_name = dest_name
    elif kind == 'PIGGY_YIELD':
        type_label = 'Rendimento'
        type_color = 'emerald'
        amount_prefix = '+ '
        origin_name = 'Cofrinho CDI'
        dest_name = (tx.dest.owner.name or tx.dest.owner.username) if tx.dest else '—'
        actor_name = dest_name
    elif kind == 'WITHDRAW' or tx.dest_id is None:
        type_label = 'Saque'
        type_color = 'coral'
        amount_prefix = '- '
        origin_name = (tx.src.owner.name or tx.src.owner.username) if tx.src else '—'
        dest_name = 'Caixa Banco24Horas'
        actor_name = origin_name
    elif kind == 'PURCHASE':
        type_label = 'Compra'
        type_color = 'purple'
        amount_prefix = ''
        origin_name = (tx.src.owner.name or tx.src.owner.username) if tx.src else '—'
        dest_name = (tx.dest.owner.name or tx.dest.owner.username) if tx.dest else '—'
        actor_name = origin_name
    else:
        type_label = 'Transferência'
        type_color = 'purple'
        amount_prefix = ''
        origin_name = (tx.src.owner.name or tx.src.owner.username) if tx.src else '—'
        dest_name = (tx.dest.owner.name or tx.dest.owner.username) if tx.dest else '—'
        actor_name = origin_name

    return {
        'id': tx.pk,
        'date_str': local_dt.strftime('%d/%m/%Y'),
        'time_str': local_dt.strftime('%H:%M'),
        'relative': format_relative_pt(tx.timestamp),
        'kind': kind,
        'type_label': type_label,
        'type_color': type_color,
        'amount': tx.amount,
        'amount_prefix': amount_prefix,
        'actor_name': actor_name,
        'origin_name': origin_name,
        'origin_iban': tx.src.iban if tx.src else '',
        'dest_name': dest_name,
        'dest_iban': tx.dest.iban if tx.dest else '',
    }


class AdminOverviewView(StaffRequiredMixin, TemplateView):
    template_name = 'admin_panel/overview.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        non_staff = Client.objects.filter(is_staff=False)
        total_clients = non_staff.count()
        pending_qs = non_staff.filter(approval_status='PENDING').order_by('-date_joined')
        pending_count = pending_qs.count()
        total_balance = Account.objects.filter(closed=None).aggregate(total=Sum('balance'))['total'] or Decimal('0.00')

        pending_list = [
            {
                'id': c.pk,
                'name': c.name or c.username,
                'doc': format_doc(c.username),
                'doc_label': 'CNPJ' if len(''.join(ch for ch in c.username if ch.isdigit())) == 14 else 'CPF',
                'account_type': c.account_type or 'Corrente',
                'is_pj': (c.account_type or '').upper() == 'PJ' or len(''.join(ch for ch in c.username if ch.isdigit())) == 14,
            }
            for c in pending_qs[:6]
        ]

        recent_txs = [
            _format_global_tx(tx)
            for tx in Transaction.objects.select_related('src__owner', 'dest__owner').order_by('-timestamp', '-pk')[:6]
        ]

        ctx.update({
            'admin_nav': 'overview',
            'total_clients': total_clients,
            'pending_count': pending_count,
            'total_balance': total_balance,
            'pending_list': pending_list,
            'recent_txs': recent_txs,
        })
        return ctx


class AdminClientsView(StaffRequiredMixin, TemplateView):
    template_name = 'admin_panel/clients.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get('q', '').strip()
        qs = Client.objects.filter(is_staff=False).prefetch_related('account_set').order_by('-date_joined', '-pk')
        if q:
            q_digits = normalize_cpf_username(q)
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(username__icontains=q) |
                Q(username__icontains=q_digits) |
                Q(email__icontains=q) |
                Q(account__iban__icontains=q)
            ).distinct()

        paginator = Paginator(qs, 10)
        page_obj = paginator.get_page(self.request.GET.get('page', 1))

        rows = []
        for client in page_obj.object_list:
            accounts = list(client.account_set.all().order_by('closed', 'pk'))
            open_accs = [a for a in accounts if a.is_open()]
            primary = open_accs[0] if open_accs else (accounts[0] if accounts else None)
            total_bal = sum((a.balance for a in open_accs), Decimal('0.00'))
            if client.approval_status == 'PENDING':
                status_label, status_tone = 'Pendente', 'pending'
            elif client.approval_status == 'REJECTED' or not client.is_active:
                status_label, status_tone = 'Bloqueada', 'closed'
            elif open_accs:
                status_label, status_tone = 'Ativa', 'active'
            else:
                status_label, status_tone = 'Sem conta', 'pending'

            rows.append({
                'client': client,
                'name': client.name or client.username,
                'doc': format_doc(client.username),
                'account_type': client.account_type or 'Corrente',
                'primary_iban': primary.iban if primary else '—',
                'accounts_count': len(accounts),
                'open_accounts_count': len(open_accs),
                'total_balance': total_bal,
                'status_label': status_label,
                'status_tone': status_tone,
                'preapproved_limit': client.preapproved_limit if client.preapproved_limit is not None else Decimal('10000.00'),
                'accounts_json': json.dumps([
                    {
                        'id': a.pk,
                        'iban': a.iban,
                        'balance': format(a.balance, '.2f'),
                        'available': format(a.available_balance, '.2f'),
                        'reserved': format(a.reserved_balance, '.2f'),
                        'is_open': a.is_open(),
                    }
                    for a in accounts
                ]),
            })

        ctx.update({
            'admin_nav': 'clients',
            'query': q,
            'rows': rows,
            'page_obj': page_obj,
            'total_records': paginator.count,
        })
        return ctx


class AdminApprovalsView(StaffRequiredMixin, TemplateView):
    template_name = 'admin_panel/approvals.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        pending_qs = list(Client.objects.filter(is_staff=False, approval_status='PENDING').order_by('date_joined', 'pk'))

        selected_id = self.request.GET.get('id')
        selected = None
        if selected_id and selected_id.isdigit():
            selected = next((c for c in pending_qs if c.pk == int(selected_id)), None)
        if selected is None and pending_qs:
            selected = pending_qs[0]

        queue = []
        for c in pending_qs:
            local_dt = timezone.localtime(c.date_joined) if timezone.is_aware(c.date_joined) else c.date_joined
            queue.append({
                'id': c.pk,
                'name': c.name or c.username,
                'initials': compute_initials(c),
                'doc': format_doc(c.username),
                'birth_date': c.birth_date or 'Não informado',
                'phone_number': c.phone_number or 'Não informado',
                'email': c.email or 'Não informado',
                'account_type': c.account_type or 'Corrente',
                'joined_str': local_dt.strftime('%d/%m/%Y às %H:%M'),
                'is_selected': selected is not None and c.pk == selected.pk,
            })

        selected_item = next((item for item in queue if item['is_selected']), None)

        ctx.update({
            'admin_nav': 'approvals',
            'queue': queue,
            'pending_count': len(queue),
            'selected': selected_item,
        })
        return ctx


class AdminTransactionsView(StaffRequiredMixin, TemplateView):
    template_name = 'admin_panel/transactions.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get('q', '').strip()
        tx_type = self.request.GET.get('type', 'ALL').upper()

        qs = Transaction.objects.select_related('src__owner', 'dest__owner').order_by('-timestamp', '-pk')
        if tx_type in {'DEPOSIT', 'WITHDRAW', 'TRANSFER', 'PURCHASE', 'PIGGY_YIELD'}:
            qs = qs.filter(transaction_type=tx_type)
        if q:
            qs = qs.filter(
                Q(src__owner__name__icontains=q) |
                Q(dest__owner__name__icontains=q) |
                Q(src__iban__icontains=q) |
                Q(dest__iban__icontains=q) |
                Q(src__owner__username__icontains=q) |
                Q(dest__owner__username__icontains=q)
            )

        paginator = Paginator(qs, 10)
        page_obj = paginator.get_page(self.request.GET.get('page', 1))
        rows = [_format_global_tx(tx) for tx in page_obj.object_list]

        ctx.update({
            'admin_nav': 'transactions',
            'query': q,
            'active_type': tx_type,
            'rows': rows,
            'page_obj': page_obj,
            'total_records': paginator.count,
        })
        return ctx


class AdminApprovalDecisionView(StaffRequiredMixin, View):
    def post(self, request, pk):
        try:
            body = json.loads(request.body.decode('utf-8') or '{}')
        except ValueError:
            body = {}
        decision = (body.get('decision') or '').upper()
        if decision not in {'APPROVE', 'REJECT'}:
            return JsonResponse({'detail': 'Decisão inválida. Use APPROVE ou REJECT.'}, status=400)

        client = get_object_or_404(Client, pk=pk, is_staff=False)
        with db_transaction.atomic():
            if decision == 'APPROVE':
                client.approval_status = 'APPROVED'
                client.is_active = True
                client.save(update_fields=['approval_status', 'is_active'])
                if not client.account_set.exists():
                    Account.objects.create(owner=client, iban=generate_unique_iban(), balance=Decimal('0.00'))
                message = f'Cadastro de {client.name or client.username} aprovado com sucesso!'
            else:
                client.approval_status = 'REJECTED'
                client.is_active = False
                client.save(update_fields=['approval_status', 'is_active'])
                message = f'Solicitação de {client.name or client.username} recusada.'

        return JsonResponse({'status': client.approval_status, 'detail': message})


class AdminClientCreateAPIView(StaffRequiredMixin, View):
    def post(self, request):
        try:
            body = json.loads(request.body.decode('utf-8') or '{}')
        except ValueError:
            body = {}

        name = (body.get('name') or '').strip()
        cpf_raw = (body.get('cpf') or '').strip()
        email = (body.get('email') or '').strip()
        phone = (body.get('phone_number') or '').strip()
        birth_date = (body.get('birth_date') or '').strip()
        account_type = (body.get('account_type') or 'Corrente').strip()
        password = body.get('password') or 'mbank1234'
        approval_status = (body.get('approval_status') or 'APPROVED').upper()
        if approval_status not in {'APPROVED', 'PENDING'}:
            approval_status = 'APPROVED'

        username = normalize_cpf_username(cpf_raw)
        if not name or not username:
            return JsonResponse({'detail': 'Informe nome completo e CPF/usuário.'}, status=400)
        if Client.objects.filter(username=username).exists():
            return JsonResponse({'detail': 'Já existe um cliente com este CPF/usuário.'}, status=409)

        with db_transaction.atomic():
            client = Client.objects.create_user(
                username=username,
                email=email,
                password=password,
                name=name,
                phone_number=phone,
                birth_date=birth_date,
                account_type=account_type,
                approval_status=approval_status,
                is_active=(approval_status == 'APPROVED'),
            )
            iban = None
            if approval_status == 'APPROVED':
                acc = Account.objects.create(owner=client, iban=generate_unique_iban(), balance=Decimal('0.00'))
                iban = acc.iban

        return JsonResponse({
            'id': client.pk,
            'name': client.name,
            'username': client.username,
            'iban': iban,
            'approval_status': client.approval_status,
        }, status=201)


class AdminClientUpdateAPIView(StaffRequiredMixin, View):
    def post(self, request, pk):
        try:
            body = json.loads(request.body.decode('utf-8') or '{}')
        except ValueError:
            body = {}

        client = get_object_or_404(Client, pk=pk, is_staff=False)
        action = (body.get('action') or 'update').lower()

        if action == 'toggle_active':
            client.is_active = not client.is_active
            if client.is_active and client.approval_status != 'APPROVED':
                client.approval_status = 'APPROVED'
            client.save(update_fields=['is_active', 'approval_status'])
            return JsonResponse({
                'is_active': client.is_active,
                'detail': 'Cliente reativado.' if client.is_active else 'Cliente bloqueado.',
            })

        if action == 'open_account':
            acc = Account.objects.create(owner=client, iban=generate_unique_iban(), balance=Decimal('0.00'))
            return JsonResponse({'iban': acc.iban, 'detail': f'Nova conta {acc.iban} aberta para {client.name}.'}, status=201)

        if 'preapproved_limit' in body:
            try:
                limit = Decimal(str(body['preapproved_limit']))
                if limit < 0:
                    raise ValueError()
            except (InvalidOperation, ValueError):
                return JsonResponse({'detail': 'Limite pré-aprovado inválido.'}, status=400)
            client.preapproved_limit = limit
            client.save(update_fields=['preapproved_limit'])
            return JsonResponse({'preapproved_limit': format(limit, '.2f'), 'detail': 'Limite atualizado!'})

        return JsonResponse({'detail': 'Nenhuma alteração solicitada.'}, status=400)
