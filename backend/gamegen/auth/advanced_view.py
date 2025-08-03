from urllib import response
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from rest_framework import status, permissions
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.request import Request
from rest_framework.response import Response
from django.core.cache import cache
import hashlib
import logging
import re

# Import the SupabaseAuthService directly
from ..supabase.auth import SupabaseAuthService

# Import custom throttling classes
from .throttling import IPRateThrottle, IPBasedUserRateThrottle

logger = logging.getLogger(__name__)

auth_service = SupabaseAuthService()

import jwt
from datetime import datetime, timezone

def is_token_expired(token: str) -> bool:
    try:
        decoded = jwt.decode(token, options={"verify_signature": False})
        exp = decoded.get("exp")

        if not exp:
            return True  # No expiration info found
        
        # Compare with current UTC time
        return datetime.now(timezone.utc).timestamp() > exp
    except Exception as e:
        return True  # Consider invalid tokens as expired

def validate_email_format(email):
    """
    Validate that the email is in a correct format.
    
    Returns:
        tuple: (is_valid, error_message)
    """
    try:
        validate_email(email)
        return True, ""
    except ValidationError:
        return False, "Invalid email format"


@api_view(["POST"])
@throttle_classes([IPRateThrottle])
def signup(request: Request) -> Response:
    """
    Create a new user with email and password.
    """
    email = request.data.get("email")
    password = request.data.get("password")

    # Validate required fields
    if not email or not password:
        return Response(
            {"error": "Email and password are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Validate email format
    try:
        validate_email(email)
    except ValidationError:
        return Response(
            {"error": "Invalid email format"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Validate password strength
    if len(password) < 8:
        return Response(
            {"error": "Password must be at least 8 characters long"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Check for complexity (uppercase, lowercase, numbers, special chars)
    if not (re.search(r'[A-Z]', password) and 
            re.search(r'[a-z]', password) and 
            re.search(r'[0-9]', password) and 
            re.search(r'[!@#$%^&*(),.?":{}|<>]', password)):
        return Response(
            {"error": "Password must contain uppercase, lowercase, numbers, and special characters"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        # Call auth service to create user
        auth_result = auth_service.create_user(email=email, password=password)
        print(f"auth_result: {auth_result}")
        
        # Return success response
        sanitized_response = {
            "message": "User created successfully",
            "supabase_uid": auth_result.get("id", ""),
            "email": email,
        }

        return Response(sanitized_response, status=status.HTTP_201_CREATED)
    except Exception as e:
        logger.error("Signup error: %s", str(e))
        # Don't expose detailed error information to client
        if "already exists" in str(e).lower() or "email already registered" in str(e).lower():
            return Response(
                {"error": "User with this email already exists"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {"error": "Failed to create user"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@throttle_classes([IPRateThrottle])
def sign_in_with_email(request: Request) -> Response:
    """
    Sign in a user with email and password.
    """
    email = request.data.get("email")
    password = request.data.get("password")

    if not email or not password:
        return Response(
            {"error": "Email and password are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Validate email format
    is_valid_email, email_error = validate_email_format(email)
    if not is_valid_email:
        return Response(
            {"error": email_error},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        response = auth_service.sign_in_with_email(email=email, password=password)
        
        # Extract user data from the response
        user = None
        if 'user' in response and response['user']:
            user = {
                'id': response['user'].get('id'),
                'email': email,
                # Add other non-sensitive user fields as needed
            }
        
        # Format the response to match the expected structure
        result = {
            "session": response,  # Use the entire response as the session data
        }
        
        if user:
            result["user"] = user
            
        # Log successful sign-in (without the password)
        logger.info(f"User signed in: {email}")
 
        res = Response(result, status=status.HTTP_200_OK)

        access_token = response.get("access_token")
        refresh_token = response.get("refresh_token")
        print(f"access_token: {access_token}")
        print(f"refresh_token: {refresh_token}")

        # Set tokens as HTTP-only cookies
        # Access Token: expires in 1 hour (example)
        res.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            secure=True,             # True in production
            samesite="Strict",          # or "Strict" / "None" based on CORS setup
            max_age=3600,            # 1 hour
        )

        # Refresh Token: longer expiration (e.g. 30 days)
        res.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=True,
            samesite="Strict",
            max_age=60 * 60 * 24 * 30,  # 30 days
        )

        return res
    except Exception as e:
        error_message = str(e)
        logger.warning(f"Sign-in failed for user {email}: {error_message}")
        
        # Return appropriate error messages based on the exception
        if "invalid login credentials" in error_message or "invalid email or password" in error_message:
            return Response(
                {"error": "Invalid email or password"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        elif "email not confirmed" in error_message:
            return Response(
                {"error": "Email not confirmed. Please check your inbox and confirm your email"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        else:
            return Response(
                {"error": "Authentication failed"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        

@api_view(["POST"])
def sign_out(request: Request) -> Response:
    """
    Sign out a user.
    """
    # Try to get auth token from request data first
    auth_token = request.data.get("auth_token")
    
    # If not in request data, try to get from Authorization header
    if not auth_token:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            auth_token = auth_header.split(' ')[1]

    # get cookie
    auth_token = request.COOKIES.get("access_token", "")
    
    if not auth_token:
        return Response(
            {"error": "Auth token is required"}, status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        auth_service.sign_out(auth_token=auth_token)
        # Return 204 No Content on successful logout as per REST conventions
        res = Response(status=status.HTTP_204_NO_CONTENT)

        res.delete_cookie("access_token")
        res.delete_cookie("refresh_token")

        return res
    except Exception as e:
        error_message = str(e)
        # Check if the error is related to authentication
        if "401" in error_message or "403" in error_message or "session_not_found" in error_message:
            # If the session is already invalid, we can consider this a successful logout
            # This handles the case where the token is expired or already invalidated
            print(f"Warning: Session already invalid during logout: {error_message}")
            return Response(status=status.HTTP_204_NO_CONTENT)
        else:
            return Response(
                {"error": f"Failed to sign out: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

@api_view(["GET"])
def get_session(request: Request) -> Response:
    """
    Retrieve the user's session.
    """

    auth_token = request.COOKIES.get("access_token", "")

    if not auth_token:
        return Response(
            {"error": "Auth token is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    try:
        response = auth_service.get_session(auth_token=auth_token)
        return Response(response, status=status.HTTP_200_OK)
    except Exception as e:
        return Response(
            {"error": f"Failed to get session: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

@api_view(["POST"])
def refresh_session(request: Request) -> Response:
    """
    Refresh a user's session using a refresh token.
    """
    refresh_token = request.COOKIES.get("refresh_token", "")

    if not refresh_token:
        return Response(
            {"error": "Refresh token is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    try:
        response = auth_service.refresh_session(refresh_token=refresh_token)

        access_token = response.get("access_token")
        refresh_token = response.get("refresh_token")

        res = Response(response, status=status.HTTP_200_OK)

        # Set tokens as HTTP-only cookies
        # Access Token: expires in 1 hour (example)
        res.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            secure=True,             # True in production
            samesite="Strict",          # or "Strict" / "None" based on CORS setup
            max_age=3600,            # 1 hour
        )

        # Refresh Token: longer expiration (e.g. 30 days)
        res.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=True,
            samesite="Strict",
            max_age=60 * 60 * 24 * 30,  # 30 days
        )

        return res
    except Exception as e:
        return Response(
            {"error": f"Failed to refresh session: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )