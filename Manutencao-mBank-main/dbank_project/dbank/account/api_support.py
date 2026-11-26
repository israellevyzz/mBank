import random
import re
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .context_processors import compute_initials
from .models import Account, Client, PurchasePasswordCounter, Transaction


def generate_unique_iban():
    for _ in range(50):
        candidate = f"BR{random.randint(10000000, 99999999)}"
        if not Account.objects.filter(iban=candidate).exists():
            return candidate
    raise RuntimeError('Unable to generate unique IBAN.')


def normalize_cpf_username(raw):
    value = (raw or '').strip()
    digits = re.sub(r'\D', '', value)
    if digits and len(digits) == 11 and re.fullmatch(r'[\d.\-\s]+', value):
        return digits
    return value


def serialize_account(account):
    ops = Transaction.objects.filter(Q(src=account) | Q(dest=account)).count()
    return {
        'id': account.pk,
        'iban': account.iban,
        'is_open': account.is_open(),
        'created': account.created,
        'closed': account.closed,
        'balance': format(account.balance, '.2f'),
        'reserved_balance': format(account.reserved_balance, '.2f'),
        'available_balance': format(account.available_balance, '.2f'),
        'transactions_count': ops,
    }


def serialize_transaction_for_user(tx, user, account_id=None):
    user_account_ids = set(Account.objects.filter(owner=user).values_list('pk', flat=True))
    if account_id is not None:
        is_in = tx.dest_id == account_id and tx.src_id != account_id
        is_out = tx.src_id == account_id and tx.dest_id != account_id
    else:
        is_in = tx.dest_id in user_account_ids and tx.src_id not in user_account_ids
        is_out = tx.src_id in user_account_ids and tx.dest_id not in user_account_ids
        if tx.src_id in user_account_ids and tx.dest_id in user_account_ids:
            is_in = False
            is_out = True

    kind = tx.transaction_type or tx.get_type().upper()
    if kind == 'DEPOSIT' or tx.src_id is None and kind != 'PIGGY_YIELD':
        direction = 'in'
        title = 'Depósito em Conta'
        subtitle = f'Conta {tx.dest.iban}' if tx.dest else 'Depósito'
        icon_kind = 'deposit'
    elif kind == 'PIGGY_YIELD':
        direction = 'in'
        title = 'Rendimento do Cofrinho'
        subtitle = f'100% CDI · {tx.dest.iban}' if tx.dest else '100% CDI'
        icon_kind = 'yield'
    elif kind == 'WITHDRAW' or tx.dest_id is None:
        direction = 'out'
        title = 'Saque Caixa 24h'
        subtitle = f'Conta {tx.src.iban}' if tx.src else 'Saque'
        icon_kind = 'withdraw'
    elif kind == 'PURCHASE':
        direction = 'out' if is_out else 'in'
        counterpart = tx.dest.owner.name if direction == 'out' and tx.dest else (tx.src.owner.name if tx.src else 'Compra')
        title = 'Compra confirmada'
        subtitle = counterpart or 'Estabelecimento'
        icon_kind = 'transfer_out' if direction == 'out' else 'transfer_in'
    else:
        if is_in:
            direction = 'in'
            counterpart = (tx.src.owner.name or tx.src.owner.username) if tx.src else 'Transferência'
            title = f'PIX recebido · {counterpart}'
            subtitle = f'{tx.dest.iban}' if tx.dest else ''
            icon_kind = 'transfer_in'
        else:
            direction = 'out'
            counterpart = (tx.dest.owner.name or tx.dest.owner.username) if tx.dest else 'Transferência'
            title = f'PIX enviado · {counterpart}'
            subtitle = f'{tx.dest.iban}' if tx.dest else ''
            icon_kind = 'transfer_out'

    return {
        'id': tx.pk,
        'timestamp': tx.timestamp,
        'amount': format(tx.amount, '.2f'),
        'transaction_type': kind,
        'direction': direction,
        'title': title,
        'subtitle': subtitle,
        'icon_kind': icon_kind,
        'src_iban': tx.src.iban if tx.src else None,
        'dest_iban': tx.dest.iban if tx.dest else None,
    }


def _parse_request_key(request):
    raw = request.headers.get('Idempotency-Key') or request.data.get('request_key') or request.data.get('idempotency_key')
    if not raw:
        return uuid4()
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return uuid4()


def verify_operation_password(request, password):
    """Validate the authenticated user's password with brute-force lockout protection."""
    now = timezone.now()
    window = timedelta(seconds=settings.PURCHASE_PASSWORD_WINDOW_SECONDS)
    origin = salted_hmac('purchase-origin', request.META.get('REMOTE_ADDR', 'unknown')).hexdigest()
    with db_transaction.atomic():
        counters = []
        for scope in [f'customer:{request.user.pk}', f'origin:{origin}']:
            counter, _ = PurchasePasswordCounter.objects.select_for_update().get_or_create(
                scope=scope,
                defaults={'window_start': now},
            )
            counters.append(counter)
        until = max((c.blocked_until for c in counters if c.blocked_until), default=None)
        if until and until > now:
            return Response(
                {
                    'code': 'PASSWORD_BLOCKED',
                    'detail': 'Muitas tentativas incorretas. Aguarde para tentar novamente.',
                    'retry_at': until,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if password and request.user.check_password(password):
            PurchasePasswordCounter.objects.filter(pk=counters[0].pk).delete()
            return None
        for counter in counters:
            if counter.blocked_until or counter.window_start + window <= now:
                counter.failures = 0
                counter.window_start = now
                counter.blocked_until = None
            counter.failures += 1
            if counter.failures >= settings.PURCHASE_PASSWORD_MAX_ATTEMPTS:
                counter.blocked_until = now + timedelta(seconds=settings.PURCHASE_PASSWORD_BLOCK_SECONDS)
            counter.save()
        if any(c.blocked_until for c in counters):
            return Response(
                {
                    'code': 'PASSWORD_BLOCKED',
                    'detail': 'Muitas tentativas incorretas. Aguarde para tentar novamente.',
                    'retry_at': max(c.blocked_until for c in counters if c.blocked_until),
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        return Response(
            {'code': 'INVALID_PASSWORD', 'detail': 'Senha incorreta. Confira sua senha e tente novamente.'},
            status=status.HTTP_400_BAD_REQUEST,
        )


class AmountInput(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))


class WithdrawInput(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))
    password = serializers.CharField(trim_whitespace=False, write_only=True, max_length=4096)


class TransferInput(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))
    dest_iban = serializers.CharField(max_length=128, required=False, allow_blank=True)
    dest_account_id = serializers.IntegerField(min_value=1, required=False)
    password = serializers.CharField(trim_whitespace=False, write_only=True, max_length=4096)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        accounts = list(Account.objects.filter(owner=user).order_by('pk'))
        open_accounts = [a for a in accounts if a.is_open()]
        return Response({
            'id': user.pk,
            'username': user.username,
            'name': user.name or user.username,
            'initials': compute_initials(user),
            'email': user.email,
            'phone_number': user.phone_number,
            'default_account_id': open_accounts[0].pk if open_accounts else None,
            'accounts': [serialize_account(a) for a in accounts],
            'total_balance_all_accounts': format(sum((a.balance for a in open_accounts), Decimal('0.00')), '.2f'),
            'total_reserved_all_accounts': format(sum((a.reserved_balance for a in open_accounts), Decimal('0.00')), '.2f'),
            'total_available_all_accounts': format(sum((a.available_balance for a in open_accounts), Decimal('0.00')), '.2f'),
        })


class AccountDepositView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        account = get_object_or_404(Account, pk=pk, owner=request.user)
        form = AmountInput(data=request.data)
        form.is_valid(raise_exception=True)
        key = _parse_request_key(request)
        try:
            entry = account.deposit(form.validated_data['amount'], actor=request.user, key=key)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        account.refresh_from_db()
        return Response({
            'account': serialize_account(account),
            'transaction': serialize_transaction_for_user(entry, request.user, account.pk),
            'pix_code': f"00020126580014br.gov.bcb.pix0136mbank-dep-{account.iban}-{entry.pk:06d}520400005303986",
        }, status=status.HTTP_201_CREATED)


@method_decorator(sensitive_post_parameters('password'), name='dispatch')
class AccountWithdrawView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        account = get_object_or_404(Account, pk=pk, owner=request.user)
        form = WithdrawInput(data=request.data)
        form.is_valid(raise_exception=True)
        password_failure = verify_operation_password(request, form.validated_data['password'])
        if password_failure is not None:
            return password_failure
        key = _parse_request_key(request)
        try:
            entry = account.withdraw(form.validated_data['amount'], actor=request.user, key=key)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        account.refresh_from_db()
        atm_code = f"{random.randint(100, 999)} {random.randint(100, 999)}"
        return Response({
            'account': serialize_account(account),
            'transaction': serialize_transaction_for_user(entry, request.user, account.pk),
            'withdrawal_code': atm_code,
        }, status=status.HTTP_201_CREATED)


@method_decorator(sensitive_post_parameters('password'), name='dispatch')
class AccountTransferView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        src = get_object_or_404(Account, pk=pk, owner=request.user)
        form = TransferInput(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data

        dest = None
        if data.get('dest_account_id'):
            dest = Account.objects.filter(pk=data['dest_account_id'], closed=None).first()
        elif data.get('dest_iban'):
            target_raw = data['dest_iban'].strip()
            dest = Account.objects.filter(iban__iexact=target_raw, closed=None).first()
            if dest is None:
                # Also allow resolving by recipient username, CPF, email or phone
                normalized = normalize_cpf_username(target_raw)
                recipient = Client.objects.filter(
                    Q(username__iexact=target_raw) |
                    Q(username=normalized) |
                    Q(email__iexact=target_raw) |
                    Q(phone_number=target_raw)
                ).first()
                if recipient:
                    dest = Account.objects.filter(owner=recipient, closed=None).exclude(pk=src.pk).order_by('pk').first()

        if dest is None:
            return Response(
                {'detail': 'Conta de destino não encontrada. Informe um IBAN válido (ex.: BR12345678) ou chave de um cliente mBank.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        if dest.pk == src.pk:
            return Response(
                {'detail': 'Escolha uma conta de destino diferente da conta de origem.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        password_failure = verify_operation_password(request, data['password'])
        if password_failure is not None:
            return password_failure

        key = _parse_request_key(request)
        try:
            entry = src.transfer(dest, data['amount'], actor=request.user, key=key)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)

        src.refresh_from_db()
        return Response({
            'account': serialize_account(src),
            'dest_iban': dest.iban,
            'dest_owner': dest.owner.name or dest.owner.username,
            'transaction': serialize_transaction_for_user(entry, request.user, src.pk),
        }, status=status.HTTP_201_CREATED)


class AccountCloseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        account = get_object_or_404(Account, pk=pk, owner=request.user)
        if account.is_closed():
            return Response({'detail': 'Esta conta já está encerrada.'}, status=status.HTTP_409_CONFLICT)
        if account.reserved_balance > 0:
            return Response({'detail': 'Resgate os cofrinhos antes de encerrar a conta.'}, status=status.HTTP_409_CONFLICT)
        account.closed = timezone.now()
        account.save(update_fields=['closed'])
        return Response({'account': serialize_account(account)})


class StatementView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        account_id = request.query_params.get('account_id')
        days = request.query_params.get('days')
        tx_type = request.query_params.get('type')

        selected_account = None
        if account_id:
            selected_account = get_object_or_404(Account, pk=account_id, owner=user)
            qs = Transaction.objects.filter(Q(src=selected_account) | Q(dest=selected_account))
        else:
            qs = Transaction.objects.filter(Q(src__owner=user) | Q(dest__owner=user))

        if days:
            try:
                days_int = int(days)
                if days_int > 0:
                    qs = qs.filter(timestamp__gte=timezone.now() - timedelta(days=days_int))
            except ValueError:
                pass

        if tx_type:
            qs = qs.filter(transaction_type=tx_type.upper())

        qs = qs.select_related('src__owner', 'dest__owner').order_by('-timestamp', '-pk')[:100]
        rows = [
            serialize_transaction_for_user(tx, user, selected_account.pk if selected_account else None)
            for tx in qs
        ]
        return Response({'results': rows})
