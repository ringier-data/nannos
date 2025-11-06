"""
Okta OIDC Authentication Middleware for A2A Protocol.

This middleware validates JWT tokens from Okta OIDC application to ensure
only authenticated users can access the A2A agent endpoints.

Authentication information is stored in request.state.user and can be accessed
by custom request handlers or middleware downstream.
"""

import logging
import os
from typing import Optional

import jwt
from jwt import PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


logger = logging.getLogger(__name__)


class OktaAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware to validate Okta OIDC JWT tokens for A2A agent requests.
    
    Configuration via environment variables:
    - OKTA_DOMAIN: Okta domain (e.g., rcplus.okta.com)
    - OKTA_CLIENT_ID: OAuth2 client ID for the OIDC application
    - OKTA_AUDIENCE: Expected audience in JWT (typically "api://default" or client ID)
    """
    
    # Public endpoints that don't require authentication
    PUBLIC_PATHS = [
        '/.well-known/agent-card.json',
        '/health',
        '/docs',
        '/openapi.json',
    ]
    
    def __init__(self, app, okta_domain: Optional[str] = None, 
                 client_id: Optional[str] = None, 
                 audience: Optional[str] = None):
        super().__init__(app)
        
        # Get configuration from environment or constructor
        self.okta_domain = okta_domain or os.getenv('OKTA_DOMAIN', 'rcplus.okta.com')
        self.client_id = client_id or os.getenv('OKTA_CLIENT_ID')
        self.audience = audience or os.getenv('OKTA_AUDIENCE', 'api://default')
        
        # Construct JWKS URI
        self.issuer = f'https://{self.okta_domain}/oauth2/default'
        self.jwks_uri = f'{self.issuer}/v1/keys'
        
        # Initialize PyJWKClient for fetching public keys
        self.jwks_client = PyJWKClient(self.jwks_uri, cache_keys=True)
        
        logger.info("Okta Auth Middleware initialized")
        logger.info(f"  Issuer: {self.issuer}")
        logger.info(f"  Client ID: {self.client_id}")
        logger.info(f"  Audience: {self.audience}")
    
    async def dispatch(self, request: Request, call_next):
        """
        Intercept requests and validate JWT token.
        """
        # Allow public endpoints without authentication
        if any(request.url.path.startswith(path) for path in self.PUBLIC_PATHS):
            return await call_next(request)
        
        # Extract Authorization header
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            logger.warning(f"Missing Authorization header for {request.url.path}")
            return JSONResponse(
                status_code=401,
                content={
                    'error': 'unauthorized',
                    'message': 'Missing Authorization header. Please authenticate with Okta OIDC.',
                    'auth_url': f'{self.issuer}/v1/authorize?client_id={self.client_id}'
                }
            )
        
        # Extract token from "Bearer <token>"
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            logger.warning(f"Invalid Authorization header format for {request.url.path}")
            return JSONResponse(
                status_code=401,
                content={
                    'error': 'invalid_token_format',
                    'message': 'Authorization header must be in format: Bearer <token>'
                }
            )
        
        token = parts[1]
        
        # Validate JWT token
        try:           
            # Decode and validate token
            if os.environ["MOCK_OKTA"] == "true":
                decoded_token = {
                    "sub": "mock-user-id",
                    "email": "mock-user@example.com",
                    "name": "Mock User",
                    "cid": self.client_id,
                    "scp": ["openid", "profile", "email"]
                }
            else:
                # Get signing key from JWKS
                signing_key = self.jwks_client.get_signing_key_from_jwt(token)
                decoded_token = jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=['RS256'],
                    audience=self.audience,
                    issuer=self.issuer,
                    options={
                        'verify_signature': True,
                        'verify_exp': True,
                        'verify_aud': True,
                        'verify_iss': True,
                    }
                )
                
            # Validate client_id if specified
            if self.client_id and decoded_token.get('cid') != self.client_id:
                logger.warning(f"Token client_id mismatch: expected {self.client_id}, got {decoded_token.get('cid')}")
                return JSONResponse(
                    status_code=403,
                    content={
                        'error': 'invalid_client',
                        'message': 'Token was not issued for this application'
                    }
                )
            
            # Add user info to request state for use in handlers
            request.state.user = {
                'sub': decoded_token.get('sub'),
                'email': decoded_token.get('email'),
                'name': decoded_token.get('name'),
                'client_id': decoded_token.get('cid'),
                'scopes': decoded_token.get('scp', []),
                'token': token,
            }
            
            logger.info(f"Authenticated user: {request.state.user.get('sub')} ({request.state.user.get('email')})")
            
            # Proceed with the request
            response = await call_next(request)
            return response
            
        except jwt.ExpiredSignatureError:
            logger.warning(f"Expired token for {request.url.path}")
            return JSONResponse(
                status_code=401,
                content={
                    'error': 'token_expired',
                    'message': 'JWT token has expired. Please re-authenticate.',
                    'auth_url': f'{self.issuer}/v1/authorize?client_id={self.client_id}'
                }
            )
        except jwt.InvalidAudienceError:
            logger.warning(f"Invalid audience in token for {request.url.path}")
            return JSONResponse(
                status_code=403,
                content={
                    'error': 'invalid_audience',
                    'message': f'Token audience does not match expected audience: {self.audience}'
                }
            )
        except jwt.InvalidIssuerError:
            logger.warning(f"Invalid issuer in token for {request.url.path}")
            return JSONResponse(
                status_code=403,
                content={
                    'error': 'invalid_issuer',
                    'message': f'Token issuer does not match expected issuer: {self.issuer}'
                }
            )
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid token for {request.url.path}: {e}")
            return JSONResponse(
                status_code=401,
                content={
                    'error': 'invalid_token',
                    'message': f'Invalid JWT token: {str(e)}'
                }
            )
        except Exception as e:
            logger.error(f"Unexpected error validating token: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={
                    'error': 'internal_error',
                    'message': 'An error occurred while validating authentication'
                }
            )
