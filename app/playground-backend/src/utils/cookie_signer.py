"""Cookie signing utilities using itsdangerous.

This module provides secure cookie signing and verification using HMAC signatures
to prevent tampering and ensure integrity of cookie values.

Key benefits:
1. Prevents tampering: Users can't modify cookie data without invalidating the signature
2. Ensures integrity: You can trust that cookie values haven't been altered
3. Maintains security: Even if users can see cookie contents, they can't forge valid signatures
4. Time-based expiration: Signatures can expire automatically

The implementation uses itsdangerous, the industry-standard library used by
Flask, Starlette, and many other Python web frameworks.
"""

from config import config
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


class CookieSigner:
    """Handles signing and verifying cookie values using HMAC signatures."""

    def __init__(self, secret_key: str | None = None):
        """Initialize the cookie signer.

        Args:
            secret_key: Secret key for signing. If None, uses config.secret_key
        """
        self._secret_key = secret_key or config.secret_key
        self._serializer = URLSafeTimedSerializer(
            secret_key=self._secret_key,
            # Use a salt to namespace signatures for cookies specifically
            salt='cookie-signature',
        )

    def sign(self, value: str) -> str:
        """Sign a cookie value.

        Args:
            value: The plain string value to sign

        Returns:
            A signed string containing both the value and its signature

        Example:
            >>> signer = CookieSigner()
            >>> signed = signer.sign('my-data')
            >>> # Returns something like: "my-data.timestamp.signature"
        """
        return self._serializer.dumps(value)

    def verify(self, signed_value: str, max_age: int | None = None) -> str | None:
        """Verify and extract the original value from a signed cookie.

        Args:
            signed_value: The signed string from the cookie
            max_age: Maximum age in seconds. If the signature is older than this,
                    it will be rejected. If None, no age check is performed.

        Returns:
            The original unsigned value if verification succeeds, None otherwise

        Example:
            >>> signer = CookieSigner()
            >>> signed = signer.sign('my-data')
            >>> original = signer.verify(signed, max_age=600)  # 10 minutes
            >>> print(original)  # "my-data"
        """
        try:
            return self._serializer.loads(signed_value, max_age=max_age)
        except SignatureExpired:
            # Signature is valid but has expired
            return None
        except BadSignature:
            # Signature is invalid (tampered with)
            return None
        except Exception:
            # Any other error (malformed data, etc.)
            return None


# Global instance using the application's secret key
cookie_signer = CookieSigner()


def sign_cookie(value: str) -> str:
    """Convenience function to sign a cookie value using the global signer.

    Args:
        value: The plain string value to sign

    Returns:
        A signed string containing both the value and its signature
    """
    return cookie_signer.sign(value)


def verify_cookie(signed_value: str, max_age: int | None = None) -> str | None:
    """Convenience function to verify a signed cookie using the global signer.

    Args:
        signed_value: The signed string from the cookie
        max_age: Maximum age in seconds for the signature

    Returns:
        The original unsigned value if verification succeeds, None otherwise
    """
    return cookie_signer.verify(signed_value, max_age=max_age)
