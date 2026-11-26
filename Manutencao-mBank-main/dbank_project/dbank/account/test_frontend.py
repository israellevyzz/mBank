from decimal import Decimal
from pathlib import Path
import re

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import Account, Client, PiggyBank


class FrontendPagesAndSupportApiTests(TestCase):
    def setUp(self):
        self.user = Client.objects.create_user(
            username='12345678900',
            password='StrongPassword!2026',
            name='Israel Levy da Silva Lima',
            email='israel@mbank.local',
        )
        self.other = Client.objects.create_user(
            username='99988877766',
            password='StrongPassword!2026',
            name='Marina Costa',
        )
        self.acc1 = Account.objects.create(owner=self.user, iban='BR12345678', balance=Decimal('8450.00'))
        self.acc2 = Account.objects.create(owner=self.user, iban='BR98765432', balance=Decimal('2150.00'))
        self.other_acc = Account.objects.create(owner=self.other, iban='BR55554444', balance=Decimal('500.00'))
        self.client.force_login(self.user)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_all_client_screens_render_200_and_exclude_expense_control(self):
        pages = [
            reverse('home'),
            reverse('bank:accounts_list'),
            reverse('bank:deposit'),
            reverse('bank:withdraw'),
            reverse('bank:transfer'),
            reverse('bank:transactions_list'),
            reverse('bank:account_transactions', args=[self.acc1.pk]),
            reverse('bank:piggy_bank'),
            reverse('bank:loan'),
        ]
        for url in pages:
            with self.subTest(url=url):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 200)
                self.assertNotContains(res, 'Controle de gastos')
                self.assertNotContains(res, 'figma.com/api/mcp/asset')

        # Cofrinho without any active piggy bank hides balance/action hero initially
        empty_piggy = self.client.get(reverse('bank:piggy_bank'))
        self.assertFalse(empty_piggy.context['has_active_piggies'])
        self.assertContains(empty_piggy, 'Criar primeiro cofrinho')

        piggy = PiggyBank.objects.create(account=self.acc1, name='Reserva de Emergência')
        populated_piggy = self.client.get(reverse('bank:piggy_bank'))
        self.assertTrue(populated_piggy.context['has_active_piggies'])

        # Guarding money in a piggy bank reduces available balance shown on Home and Minhas Contas
        res_move = self.api.post(
            f'/bank/api/cofrinhos/{piggy.pk}/guardar/',
            {'amount': '500.00', 'idempotency_key': 'piggy-home-check-1'},
            format='json',
        )
        self.assertEqual(res_move.status_code, 200)
        home_after = self.client.get(reverse('home'))
        self.assertContains(home_after, 'R$ 10.100,00')
        self.assertContains(home_after, 'R$ 500,00 guardado no Cofrinho')
        accounts_after = self.client.get(reverse('bank:accounts_list'))
        self.assertContains(accounts_after, '10.100,00')
        self.assertContains(accounts_after, 'R$ 500,00 no Cofrinho')

    def test_auth_screens_and_registration_flow(self):
        self.client.logout()
        landing = self.client.get(reverse('home'))
        self.assertEqual(landing.status_code, 200)
        self.assertContains(landing, 'Acesso Admin')
        self.assertContains(landing, 'Entrar na Conta')
        self.assertContains(landing, 'Abrir Conta')

        self.assertEqual(self.client.get(reverse('login')).status_code, 200)
        self.assertEqual(self.client.get(reverse('register')).status_code, 200)
        admin_login_page = self.client.get(reverse('admin_login'))
        self.assertEqual(admin_login_page.status_code, 200)
        self.assertContains(admin_login_page, 'Acesso Administrativo')

        # Register new client with formatted CPF -> enters Admin Master approval queue as PENDING
        res = self.client.post(reverse('register'), {
            'name': 'Ana Paula Souza',
            'cpf': '111.222.333-44',
            'birth_date': '10/05/1995',
            'phone_number': '(11) 99999-0000',
            'email': 'ana@mbank.local',
            'password': 'SafePassword#2026',
            'password_confirm': 'SafePassword#2026',
        })
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Cadastro enviado para análise')
        created = Client.objects.get(username='11122233344')
        self.assertEqual(created.name, 'Ana Paula Souza')
        self.assertEqual(created.approval_status, 'PENDING')
        self.assertFalse(created.is_active)
        self.assertEqual(created.account_set.count(), 0)

        # Login before approval is blocked with pending-analysis message
        blocked_res = self.client.post(reverse('login'), {
            'username': '111.222.333-44',
            'password': 'SafePassword#2026',
        })
        self.assertEqual(blocked_res.status_code, 200)
        self.assertContains(blocked_res, 'ainda está em análise')

        # Admin Master approves the registration -> activates client and creates IBAN account
        staff = Client.objects.create_user(
            username='admin_approver_test',
            password='AdminPassword#2026',
            name='Admin Test',
            is_staff=True,
        )
        self.client.force_login(staff)
        approve_res = self.client.post(
            reverse('admin_api_approval_decision', args=[created.pk]),
            data='{"decision": "APPROVE"}',
            content_type='application/json',
        )
        self.assertEqual(approve_res.status_code, 200)
        created.refresh_from_db()
        self.assertEqual(created.approval_status, 'APPROVED')
        self.assertTrue(created.is_active)
        self.assertEqual(created.account_set.count(), 1)

        # Login with formatted CPF now succeeds
        self.client.logout()
        login_res = self.client.post(reverse('login'), {
            'username': '111.222.333-44',
            'password': 'SafePassword#2026',
        })
        self.assertEqual(login_res.status_code, 302)

    def test_support_api_me_deposit_withdraw_transfer_close_and_isolation(self):
        me = self.api.get('/bank/api/me/').data
        self.assertEqual(me['initials'], 'IL')
        self.assertEqual(me['total_balance_all_accounts'], '10600.00')

        # Deposit
        dep = self.api.post(f'/bank/api/accounts/{self.acc1.pk}/deposito/', {'amount': '150.00'}, format='json')
        self.assertEqual(dep.status_code, 201)
        self.assertEqual(dep.data['account']['balance'], '8600.00')

        # Withdraw rejects wrong password and succeeds with valid password
        bad_wit = self.api.post(
            f'/bank/api/accounts/{self.acc1.pk}/saque/',
            {'amount': '200.00', 'password': 'wrong-password'},
            format='json',
        )
        self.assertEqual(bad_wit.status_code, 400)
        self.assertEqual(bad_wit.data['code'], 'INVALID_PASSWORD')

        wit = self.api.post(
            f'/bank/api/accounts/{self.acc1.pk}/saque/',
            {'amount': '200.00', 'password': 'StrongPassword!2026'},
            format='json',
        )
        self.assertEqual(wit.status_code, 201)
        self.assertEqual(wit.data['account']['balance'], '8400.00')
        self.assertTrue(wit.data['withdrawal_code'])

        # Transfer rejects wrong password and succeeds with valid password
        bad_trf = self.api.post(
            f'/bank/api/accounts/{self.acc1.pk}/transferencia/',
            {'dest_iban': 'BR55554444', 'amount': '400.00', 'password': 'wrong-password'},
            format='json',
        )
        self.assertEqual(bad_trf.status_code, 400)
        self.assertEqual(bad_trf.data['code'], 'INVALID_PASSWORD')

        trf = self.api.post(
            f'/bank/api/accounts/{self.acc1.pk}/transferencia/',
            {'dest_iban': 'BR55554444', 'amount': '400.00', 'password': 'StrongPassword!2026'},
            format='json',
        )
        self.assertEqual(trf.status_code, 201)
        self.acc1.refresh_from_db()
        self.other_acc.refresh_from_db()
        self.assertEqual(self.acc1.balance, Decimal('8000.00'))
        self.assertEqual(self.other_acc.balance, Decimal('900.00'))

        # Isolation: cannot deposit/withdraw/close foreign account
        self.assertEqual(
            self.api.post(
                f'/bank/api/accounts/{self.other_acc.pk}/saque/',
                {'amount': '10.00', 'password': 'StrongPassword!2026'},
                format='json',
            ).status_code,
            404,
        )
        self.assertEqual(
            self.api.post(f'/bank/api/accounts/{self.other_acc.pk}/encerrar/').status_code,
            404,
        )

        # Cannot close account with piggy reserve
        piggy = PiggyBank.objects.create(account=self.acc2, name='Reserva')
        self.api.post(f'/bank/api/cofrinhos/{piggy.pk}/guardar/', {'amount': '100.00', 'idempotency_key': 'k1'}, format='json')
        self.assertEqual(self.api.post(f'/bank/api/accounts/{self.acc2.pk}/encerrar/').status_code, 409)

        # Can close account without reserve
        self.assertEqual(self.api.post(f'/bank/api/accounts/{self.acc1.pk}/encerrar/').status_code, 200)
        self.acc1.refresh_from_db()
        self.assertTrue(self.acc1.is_closed())

    def test_all_static_assets_referenced_in_templates_exist(self):
        base_dir = Path(settings.BASE_DIR)
        templates_dir = base_dir / 'templates'
        static_dir = base_dir / 'static'
        pattern = re.compile(r"{%\s*static\s+'([^']+)'\s*%}")
        checked = 0
        for html_file in templates_dir.rglob('*.html'):
            content = html_file.read_text(encoding='utf-8')
            self.assertNotIn('figma.com/api/mcp/asset', content)
            for rel_path in pattern.findall(content):
                full_path = static_dir / rel_path
                self.assertTrue(full_path.exists(), f'Missing static asset {rel_path} referenced in {html_file}')
                self.assertGreater(full_path.stat().st_size, 0, f'Empty static asset {rel_path}')
                checked += 1
        self.assertGreater(checked, 40)


class AdminPanelTests(TestCase):
    def setUp(self):
        self.admin = Client.objects.create_user(
            username='admin_master',
            password='AdminPassword!2026',
            name='Administrador mBank',
            is_staff=True,
            is_superuser=True,
        )
        self.regular = Client.objects.create_user(
            username='22233344455',
            password='UserPassword!2026',
            name='Cliente Comum',
        )
        self.pending = Client.objects.create_user(
            username='38492180844',
            password='UserPassword!2026',
            name='Lucas Mendes',
            email='lucas@mbank.local',
            phone_number='(11) 98741-2099',
            birth_date='18/08/2005',
            account_type='Corrente',
            approval_status='PENDING',
            is_active=False,
        )

    def test_staff_access_and_login_redirect(self):
        # Regular user gets 403 on admin panel
        self.client.force_login(self.regular)
        self.assertEqual(self.client.get(reverse('admin_overview')).status_code, 403)
        self.client.logout()

        # Regular user cannot log in through admin_login
        denied = self.client.post(reverse('admin_login'), {
            'username': '22233344455',
            'password': 'UserPassword!2026',
        })
        self.assertEqual(denied.status_code, 200)
        self.assertContains(denied, 'permissão de administrador')

        # Staff login via /painel-admin/login/ redirects to /painel-admin/
        res = self.client.post(reverse('admin_login'), {
            'username': 'admin_master',
            'password': 'AdminPassword!2026',
        })
        self.assertRedirects(res, reverse('admin_overview'))

        # All 4 admin screens render 200
        for name in ['admin_overview', 'admin_clients', 'admin_approvals', 'admin_transactions']:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_approval_decision_and_client_management(self):
        self.client.force_login(self.admin)

        # Approve pending applicant
        approve_res = self.client.post(
            reverse('admin_api_approval_decision', args=[self.pending.pk]),
            data='{"decision": "APPROVE"}',
            content_type='application/json',
        )
        self.assertEqual(approve_res.status_code, 200)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.approval_status, 'APPROVED')
        self.assertTrue(self.pending.is_active)
        self.assertEqual(self.pending.account_set.count(), 1)

        # Create a new pending client via admin API and reject it
        create_res = self.client.post(
            reverse('admin_api_client_create'),
            data='{"name": "Empresa Teste", "cpf": "12345678000190", "account_type": "PJ", "approval_status": "PENDING"}',
            content_type='application/json',
        )
        self.assertEqual(create_res.status_code, 201)
        new_id = create_res.json()['id']

        reject_res = self.client.post(
            reverse('admin_api_approval_decision', args=[new_id]),
            data='{"decision": "REJECT"}',
            content_type='application/json',
        )
        self.assertEqual(reject_res.status_code, 200)
        rejected = Client.objects.get(pk=new_id)
        self.assertEqual(rejected.approval_status, 'REJECTED')
        self.assertFalse(rejected.is_active)

        # Update preapproved limit and open new account
        limit_res = self.client.post(
            reverse('admin_api_client_update', args=[self.regular.pk]),
            data='{"preapproved_limit": "25000.00"}',
            content_type='application/json',
        )
        self.assertEqual(limit_res.status_code, 200)
        self.regular.refresh_from_db()
        self.assertEqual(self.regular.preapproved_limit, Decimal('25000.00'))

        acc_res = self.client.post(
            reverse('admin_api_client_update', args=[self.regular.pk]),
            data='{"action": "open_account"}',
            content_type='application/json',
        )
        self.assertEqual(acc_res.status_code, 201)
        self.assertEqual(self.regular.account_set.count(), 1)
