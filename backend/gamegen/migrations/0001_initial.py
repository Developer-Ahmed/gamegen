import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StoredGame",
            fields=[
                ("id", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("title", models.CharField(default="Generated game", max_length=255)),
                ("thread_id", models.CharField(blank=True, db_index=True, max_length=255, null=True)),
                ("entry_path", models.CharField(max_length=500)),
                ("file_count", models.PositiveIntegerField(default=0)),
                ("test_report", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="UserProfile",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        help_text="Unique identifier for the profile",
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "supabase_uid",
                    models.CharField(
                        help_text="Unique identifier from Supabase",
                        max_length=255,
                        unique=True,
                        verbose_name="Supabase UID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "User Profile",
                "verbose_name_plural": "User Profiles",
            },
        ),
        migrations.CreateModel(
            name="StoredGameFile",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("path", models.CharField(max_length=500)),
                ("content_type", models.CharField(default="application/octet-stream", max_length=120)),
                ("content", models.BinaryField()),
                ("size", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "game",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="files",
                        to="gamegen.storedgame",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="storedgamefile",
            index=models.Index(fields=["game", "path"], name="gamegen_sto_game_id_477c39_idx"),
        ),
        migrations.AlterUniqueTogether(
            name="storedgamefile",
            unique_together={("game", "path")},
        ),
    ]
