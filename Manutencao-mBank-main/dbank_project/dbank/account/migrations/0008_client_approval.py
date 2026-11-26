from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('account', '0007_purchase_confirmation'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='account_type',
            field=models.CharField(blank=True, default='Corrente', max_length=30, verbose_name='account type'),
        ),
        migrations.AddField(
            model_name='client',
            name='approval_status',
            field=models.CharField(
                choices=[('APPROVED', 'Aprovado'), ('PENDING', 'Pendente'), ('REJECTED', 'Rejeitado')],
                default='APPROVED',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='client',
            name='birth_date',
            field=models.CharField(blank=True, default='', max_length=20, verbose_name='birth date'),
        ),
    ]
