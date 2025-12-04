"""Authentication controller for OIDC flow using Authlib."""

import logging

from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth, OAuthError
from config import config
from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from services.session_service import SessionService
from services.user_service import UserService
from utils.cookie_signer import sign_cookie


logger = logging.getLogger(__name__)


# Initialize OAuth registry
oauth = OAuth()


def register_oauth_provider() -> None:
    """Register OIDC as OAuth provider with Authlib.

    This should be called once during application startup.
    """
    oidc_config = config.oidc

    # Build the server metadata URL for automatic configuration
    issuer = oidc_config.issuer
    server_metadata_url = f'{issuer}/.well-known/openid-configuration'

    oauth.register(
        name='oidc',
        client_id=oidc_config.client_id,
        client_secret=oidc_config.client_secret.get_secret_value(),
        server_metadata_url=server_metadata_url,
        client_kwargs={
            'scope': oidc_config.scope,
            'code_challenge_method': 'S256',  # Enable PKCE
        },
    )
    logger.info('Registered OIDC OAuth provider with Authlib')


class AuthController:
    """Handles authentication flow with OIDC using Authlib."""

    def __init__(
        self,
        session_service: SessionService,
        user_service: UserService,
    ) -> None:
        """Initialize the auth controller."""
        self.session_service = session_service
        self.user_service = user_service
        self.oidc_config = config.oidc
        self.base_domain = config.base_domain
        self.is_dev = config.is_local() or config.is_dev()

    def _is_valid_redirect_url(self, url: str | None) -> bool:
        """Validate that the redirect URL is safe."""
        try:
            if not url:
                return False

            # Prevent redirect loops - reject any auth-related URLs
            auth_paths = [
                '/api/v1/auth/login',
                '/api/v1/auth/login-callback',
                '/api/v1/auth/logout',
                '/api/v1/auth/logout-callback',
            ]
            for auth_path in auth_paths:
                if auth_path in url:
                    logger.warning(f'Rejected redirect URL containing auth path: {url}')
                    return False

            # Simple validation - in production, be more strict
            if url.startswith('http://') and self.is_dev and self.base_domain in url:
                return True
            if config.is_local():
                return True
            return url.startswith('https://') and self.base_domain in url
        except Exception:
            return False

    async def get_login(self, request: Request) -> RedirectResponse:
        """Initiate the OIDC login flow.

        Query params:
            redirectTo: URL to redirect to after successful login
        """
        redirect_to = request.query_params.get('redirectTo')

        logger.debug(f'Login initiated with redirectTo: {redirect_to}')

        if not self._is_valid_redirect_url(redirect_to):
            logger.warning(f'Invalid redirectTo URL rejected: {redirect_to}')
            raise HTTPException(status_code=422, detail='Invalid redirectTo URL')

        # Store redirect_to in session for retrieval after callback
        request.session['redirect_to'] = redirect_to

        # Use Authlib to handle the OAuth flow (PKCE is automatic)
        redirect_uri = request.url_for('login_callback')
        try:
            return await oauth.oidc.authorize_redirect(request, redirect_uri)
        except Exception as e:
            logger.error(f'Failed to initiate OAuth flow: {e}')
            raise HTTPException(status_code=500, detail='Failed to initiate login') from e

    async def get_login_callback(self, request: Request, response: Response) -> RedirectResponse:
        """Handle the OIDC login callback.

        Query params:
            code: Authorization code from OIDC
            state: State parameter to prevent CSRF
            error: Error code if authorization failed
            error_description: Human-readable error description
        """
        logger.info('Login callback invoked')

        # Get redirect_to from session and validate it
        redirect_to: str | None = request.session.get('redirect_to')
        logger.debug(f'Retrieved redirect_to from session: {redirect_to}')

        # Clear redirect_to from session immediately to prevent reuse
        request.session.pop('redirect_to', None)
        logger.debug('Cleared redirect_to from session')

        # Validate redirect URL to prevent loops - ensure it's always a valid string
        if not self._is_valid_redirect_url(redirect_to):
            logger.warning(f'Invalid or missing redirect_to in session: {redirect_to}')
            redirect_to = request.url_for('index')

        logger.info(f'Will redirect to: {redirect_to} after successful login')

        # Use Authlib to handle token exchange and validation
        try:
            token = await oauth.oidc.authorize_access_token(request)
        except OAuthError as e:
            logger.error(f'OAuth error in callback: {e.error} - {e.description}')
            # Clear any remaining session state on error
            request.session.clear()
            raise HTTPException(
                status_code=400,
                detail=f'Authorization failed: {e.description}',
            ) from e
        except Exception as e:
            logger.error(f'Unexpected error during token exchange: {e}')
            # Clear any remaining session state on error
            request.session.clear()
            raise HTTPException(status_code=500, detail='Token exchange failed') from e

        # Authlib automatically validates the ID token and extracts userinfo
        userinfo = token.get('userinfo')
        if not userinfo:
            logger.error('No userinfo in token response')
            raise HTTPException(status_code=400, detail='Missing user information')

        # Extract user data
        sub = userinfo.get('sub')
        email = userinfo.get('email', '')
        given_name = userinfo.get('given_name', '')
        family_name = userinfo.get('family_name', '')
        company_name = userinfo.get('company_name')

        if not sub or not email:
            logger.error('Missing required user info')
            raise HTTPException(status_code=400, detail='Missing user information')

        # Upsert user
        user = await self.user_service.upsert_user(
            sub=sub,
            email=email,
            first_name=given_name,
            last_name=family_name,
            company_name=company_name,
        )

        # Get tokens for session
        access_token = token.get('access_token', '')
        refresh_token = token.get('refresh_token', '')
        id_token = token.get('id_token', '')
        logger.debug(f'Expires in: {token.get("expires_in")}')
        expires_in = token.get('expires_in', 3600)  # Default to 1 hour if not provided

        # Create session (store access_token for token exchange)
        session_id = await self.session_service.create_session(
            user_id=user.id,
            refresh_token=refresh_token,
            id_token=id_token,
            access_token=access_token,
            access_token_expires_in=expires_in,
        )

        # Create redirect response
        redirect_response = RedirectResponse(url=redirect_to, status_code=303)

        # Sign the session ID to prevent tampering
        signed_session_id = sign_cookie(session_id)

        # Set signed session cookie
        redirect_response.set_cookie(
            key=config.cookie_name,
            value=signed_session_id,
            path='/',  # Changed from '/api/' to '/' so cookie is sent to all routes
            httponly=True,
            secure=not config.is_local(),
            samesite='lax',  # Must be 'lax' to allow cookie on OAuth redirects from Keycloak
            max_age=config.session_ttl_seconds,
        )
        logger.debug(f'Redirecting user to: {redirect_to}')
        return redirect_response

    async def get_logout(self, request: Request) -> RedirectResponse:
        """Initiate the logout flow.

        Query params:
            redirectTo: URL to redirect to after logout (optional)
        """
        redirect_to = request.query_params.get('redirectTo')

        if not self._is_valid_redirect_url(redirect_to):
            redirect_to = request.url_for('index')

        # Destroy session if exists
        session_id = getattr(request.state, 'session_id', None)
        id_token = None
        if session_id:
            # Get the session to retrieve the id_token
            session = await self.session_service.get_session(session_id)
            if session:
                id_token = session.id_token
            # Destroy the session
            await self.session_service.destroy_session(session_id)

        # Store redirect_to in session for after logout (convert to string for JSON serialization)
        request.session['logout_redirect_to'] = str(redirect_to)

        # Get end_session_endpoint from Authlib's loaded server metadata
        oidc_client = oauth.oidc  # type: ignore[attr-defined]
        await oidc_client.load_server_metadata()  # type: ignore[attr-defined]
        end_session_endpoint = oidc_client.server_metadata.get('end_session_endpoint')  # type: ignore[attr-defined]

        # Build logout URL
        logout_params = {
            'post_logout_redirect_uri': request.url_for('logout_callback'),
            'id_token_hint': id_token,
        }
        logout_url = f'{end_session_endpoint}?{urlencode(logout_params)}'

        # Create response
        response = RedirectResponse(url=logout_url, status_code=303)

        # Clear custom session cookie (user is logged out)
        response.delete_cookie(
            key=config.cookie_name,
            path='/',
        )

        # Don't clear Starlette session cookie yet - we need it to persist logout_redirect_to
        # through the OIDC redirect roundtrip. It will be cleared in the logout callback.

        logger.info('User logging out')
        return response

    async def get_logout_callback(self, request: Request) -> RedirectResponse:
        """Handle the OIDC logout callback.

        Returns:
            Redirect to the originally requested page
        """
        # Get redirect URL from session
        redirect_to = request.session.pop('logout_redirect_to', None)

        # Validate redirect URL for safety
        if not self._is_valid_redirect_url(redirect_to):
            redirect_to = request.url_for('index')

        # Clear any remaining session data
        request.session.clear()

        # Create response
        response = RedirectResponse(url=redirect_to, status_code=303)

        # Now clear the Starlette session cookie (we're done with the logout flow)
        response.delete_cookie(
            key='session',
            path='/api/v1/auth/',
        )

        logger.info('Logout callback completed')
        return response
