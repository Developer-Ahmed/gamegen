import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import gamegen.models.generated_game


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("gamegen", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="GameChat",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(default="New game", max_length=120)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("generating", "Generating"),
                            ("completed", "Completed"),
                            ("stopped", "Stopped"),
                        ],
                        default="active",
                        max_length=16,
                    ),
                ),
                ("execution_state", models.JSONField(default=gamegen.models.generated_game.default_execution_state)),
                ("sandbox_id", models.CharField(blank=True, default="", max_length=120)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="game_chats",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-updated_at"]},
        ),
    ]
