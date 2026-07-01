"""Socket.IO event names as constants to avoid magic strings."""


class SocketEvents:
    """Standard Socket.IO event names."""

    # Connection lifecycle
    CONNECT = "connect"
    DISCONNECT = "disconnect"

    # Client initialization
    INITIALIZE_CLIENT = "initialize_client"
    CLIENT_INITIALIZED = "client_initialized"

    # Messaging
    SEND_MESSAGE = "send_message"
    AGENT_RESPONSE = "agent_response"
    CANCEL_TASK = "cancel_task"

    # Conversation stream subscription (resume after reconnect / reload; multi-tab)
    # A client joins the room for the conversation it is viewing; the backend delivers
    # a conversation's live stream to that room (keyed by conversation_id) rather than
    # to a single ephemeral connection, and replies with a snapshot of any in-flight turn.
    SUBSCRIBE_CONVERSATION = "subscribe_conversation"
    UNSUBSCRIBE_CONVERSATION = "unsubscribe_conversation"
    CONVERSATION_SNAPSHOT = "conversation_snapshot"

    # Debugging
    DEBUG_LOG = "debug_log"

    # Error handling
    ERROR = "error"

    # Server management
    SERVER_SHUTDOWN = "server:shutdown"
    DISCONNECT_INFO = "disconnect:info"

    # Scheduler notifications
    SCHEDULER_NOTIFICATION = "scheduler_notification"

    # Voice call notifications
    CALL_COMPLETED = "call_completed"

    # Catalog events
    CATALOG_REINDEX_PROGRESS = "catalog_reindex_progress"
    CATALOG_SYNC_PROGRESS = "catalog_sync_progress"
