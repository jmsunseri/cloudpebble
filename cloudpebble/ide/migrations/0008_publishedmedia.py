import django.core.validators
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('ide', '0007_usergithubreposync'),
    ]

    operations = [
        migrations.CreateModel(
            name='PublishedMedia',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, validators=[django.core.validators.RegexValidator(regex='^\\w+$', message='Invalid name.')])),
                ('glance', models.CharField(blank=True, max_length=100, null=True)),
                ('timeline_tiny', models.CharField(blank=True, max_length=100, null=True)),
                ('timeline_small', models.CharField(blank=True, max_length=100, null=True)),
                ('timeline_large', models.CharField(blank=True, max_length=100, null=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='published_media', to='ide.project')),
            ],
            options={
                'db_table': 'cloudpebble_published_media',
                'unique_together': {('project', 'name')},
            },
        ),
    ]
