"""Socket.IO event names as constants to avoid magic strings."""


class SocketEvents:
    """Standard Socket.IO event names."""

    # Connection lifecycle
    CONNECT = 'connect'
    DISCONNECT = 'disconnect'

    # Client initialization
    INITIALIZE_CLIENT = 'initialize_client'
    CLIENT_INITIALIZED = 'client_initialized'

    # Messaging
    SEND_MESSAGE = 'send_message'
    AGENT_RESPONSE = 'agent_response'

    # Debugging
    DEBUG_LOG = 'debug_log'

    # Error handling
    ERROR = 'error'

    # Server management
    SERVER_SHUTDOWN = 'server:shutdown'
    DISCONNECT_INFO = 'disconnect:info'
