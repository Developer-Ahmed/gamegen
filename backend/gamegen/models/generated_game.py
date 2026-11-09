import uuid

from django.conf import settings
from django.db import models


def default_execution_state():
    return {
        "phase": "new",
        "workspace_created": False,
        "components": [],
        "files": {},
        "workspace_sources": {},
        "required_component_reads": [],
        "required_main_reads": [],
        "read_coverage": {},
        "read_snapshots": {},
        "active_read": None,
        "read_revision": 0,
        "read_lines_covered": 0,
        "initial_context_complete": False,
        "preparation_complete": False,
        "research_completed": False,
        "research_context": {},
        "research_revision": 0,
        "research_attempts": [],
        "component_contracts": [],
        "game_title": "",
        "game_plan": "",
        "game_blueprint": {},
        "active_stage_id": "",
        "active_step_id": "",
        "stage_plan_revision": 0,
        "project_component_contracts": {},
        "game_contracts": {},
        "pending_changed_paths": [],
        "workspace_revision": 0,
        "last_valid_revision": 0,
        "candidate_revision": None,
        "candidate_base_revision": None,
        "candidate_changed_paths": [],
        "candidate_files": {},
        "candidate_sources": {},
        "candidate_stage_name": "",
        "candidate_stage_targets": [],
        "candidate_change_summary": "",
        "completed_stages": [],
        "completed_stage_targets": [],
        "candidate_failure_count": 0,
        "repair_packet": None,
        "step_correction": None,
        "step_attempt_failures": 0,
        "rejected_step_signatures": [],
        "rejected_step_strategy_fingerprints": [],
        "rejected_stage_plans": [],
        "stage_replan_counts": {},
        "last_validation_fingerprint": "",
        "attempted_edit_signatures": [],
        "blocked_stage_strategies": [],
        "stage_retry_directive": None,
        "gameplay_edit_count": 0,
        "validated_revision": -1,
        "build_revision": -1,
        "run_finished": False,
        "run_succeeded": False,
        "repairs_used": 0,
        "failure_count": 0,
        "no_progress_count": 0,
        "last_tool_signature": "",
        "last_tool_progress_token": "",
        "last_tool_error": "",
        "repeated_tool_call_count": 0,
        "terminal": False,
    }


class GameChat(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        GENERATING = "generating", "Generating"
        COMPLETED = "completed", "Completed"
        STOPPED = "stopped", "Stopped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="game_chats",
    )
    title = models.CharField(max_length=120, default="New game")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    execution_state = models.JSONField(default=default_execution_state)
    sandbox_id = models.CharField(max_length=120, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    @property
    def is_locked(self):
        return self.status == self.Status.COMPLETED

    def to_public_dict(self):
        game = StoredGame.objects.filter(thread_id=str(self.id)).first()
        return {
            "id": str(self.id),
            "title": self.title,
            "status": self.status,
            "is_locked": self.is_locked,
            "game": game.to_public_dict() if game else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class StoredGame(models.Model):
    id = models.CharField(primary_key=True, max_length=64)
    title = models.CharField(max_length=255, default="Generated game")
    thread_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    entry_path = models.CharField(max_length=500)
    file_count = models.PositiveIntegerField(default=0)
    test_report = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def play_url(self):
        return f"/games/{self.id}/"

    def to_public_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "thread_id": self.thread_id,
            "entry_path": self.entry_path,
            "file_count": self.file_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "test_report": self.test_report or {},
            "play_url": self.play_url,
        }

    def __str__(self):
        return self.title


class StoredGameFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    game = models.ForeignKey(StoredGame, on_delete=models.CASCADE, related_name="files")
    path = models.CharField(max_length=500)
    content_type = models.CharField(max_length=120, default="application/octet-stream")
    content = models.BinaryField()
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["game", "path"], name="gamegen_sto_game_id_477c39_idx"),
        ]
        unique_together = ("game", "path")

    def __str__(self):
        return f"{self.game_id}:{self.path}"
