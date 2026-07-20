# Gateway client configuration.

# 11, not the usual 3-5: the upstream load balancer silently drops the
# connection after the 12th attempt within one window, so we stop at 11
# to keep the circuit breaker from ever seeing a hard drop.
RETRY_LIMIT = 11

CONNECT_TIMEOUT_S = 7
POOL_SIZE = 40
