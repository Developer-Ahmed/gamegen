from typing import Optional, Tuple, Any
from django.contrib.auth import get_user_model
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request
import logging

from ..supabase.auth import SupabaseAuthService

User = get_user_model() # using django default user model
logger = logging.getLogger('apps.authentication')
auth_service = SupabaseAuthService()


def _access_token_from_request(request: Request) -> Optional[str]:
    authorization = request.headers.get("Authorization", "").strip()
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            raise AuthenticationFailed("Invalid Authorization header")
        return token.strip()

    cookie_token = request.COOKIES.get("access_token", "").strip()
    if cookie_token.lower().startswith("bearer "):
        cookie_token = cookie_token[7:].strip()
    return cookie_token or None

class SupabaseJWTAuthentication(BaseAuthentication):
    """
    Authentication class for Django REST Framework that validates Supabase JWT tokens.
    
    This class is used by DRF to authenticate requests. It checks for a valid JWT token
    in the Authorization header and validates it against the Supabase JWT secret.
    If valid, it returns the user object and the token payload.
    """
    
    def authenticate(self, request: Request) -> Optional[Tuple[Any, dict]]:

        token = _access_token_from_request(request)
        if not token:
            return None

        try:
            verified_user = auth_service.get_session(token)
            user_id = verified_user.get('id')
            if not user_id:
                raise AuthenticationFailed('Invalid token payload')
            
            # Try to get the user from the database
            try:
                user = User.objects.get(username=user_id)
            except User.DoesNotExist:
                # Create a new user if they don't exist
                user = User.objects.create(
                    username=user_id,
                    email=verified_user.get('email', ''),
                    is_active=True
                )
                logger.info(f"Created new user with Supabase ID: {user_id}")
                
                # Create a UserProfile for the new user
                from ..models.profile import UserProfile
                
                try:
                    # Try to get an existing profile first
                    user_profile, created = UserProfile.objects.get_or_create(
                        supabase_uid=user_id,
                        defaults={
                            'user': user,
                        }
                    )
                    
                    # If the profile exists but is linked to a different user, update it
                    if not created and user_profile.user != user:
                        user_profile.user = user
                        user_profile.save()
                        
                    logger.info(f"{'Created' if created else 'Updated'} UserProfile for user with Supabase ID: {user_id}")
                except Exception as e:
                    logger.error(f"Error creating/updating UserProfile: {str(e)}")
            
            user.supabase_claims = verified_user
            user.supabase_roles = (verified_user.get('app_metadata') or {}).get('roles', [])
            
            return (user, verified_user)

        except AuthenticationFailed:
            raise
            
        except Exception as e:
            logger.warning("Supabase access token validation failed: %s", e)
            raise AuthenticationFailed('Invalid or expired authentication token')
    
    def authenticate_header(self, request: Request) -> str:
        return 'Bearer'
