from django.db import models, transaction
from django.conf import settings
from django.utils.translation import gettext_lazy as _
import uuid

class UserProfile(models.Model):
    """
    Extended user profile model to store additional user information.
    
    This model extends the built-in Django User model with additional fields
    that are specific to our application, including Supabase-related information.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        help_text=_('Unique identifier for the profile')
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='profile'
    )
    supabase_uid = models.CharField(
        _('Supabase UID'),
        max_length=255,
        unique=True,
        help_text=_('Unique identifier from Supabase')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _('User Profile')
        verbose_name_plural = _('User Profiles')
    
    def __str__(self) -> str:
        return f"{self.user.username}'s profile"