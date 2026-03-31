from django.db import migrations


def rename_if_exists(old_name, new_name):
    return migrations.RunSQL(
        sql=f"ALTER TABLE IF EXISTS {old_name} RENAME TO {new_name}",
        reverse_sql=f"ALTER TABLE IF EXISTS {new_name} RENAME TO {old_name}",
    )


class Migration(migrations.Migration):

    dependencies = [
        ('ide', '0003_alter_project_project_dependencies_and_more'),
    ]

    operations = [
        rename_if_exists('social_auth_usersocialauth', 'cloudpebble_social_auth_usersocialauth'),
        rename_if_exists('social_auth_nonce', 'cloudpebble_social_auth_nonce'),
        rename_if_exists('social_auth_association', 'cloudpebble_social_auth_association'),
        rename_if_exists('social_auth_code', 'cloudpebble_social_auth_code'),
        rename_if_exists('social_auth_partial', 'cloudpebble_social_auth_partial'),
        rename_if_exists('registration_registrationprofile', 'cloudpebble_registration_registrationprofile'),
        rename_if_exists('registration_supervisedregistrationprofile', 'cloudpebble_registration_supervisedregistrationprofile'),
    ]
