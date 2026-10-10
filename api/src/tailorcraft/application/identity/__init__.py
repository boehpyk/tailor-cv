"""Use cases for the `identity` bounded context: starting a guest session (slice 1.1), and
registering, logging in, refreshing, logging out, reading the current user and revoking every login
(slice 2.1); requesting and confirming a registration, requesting and completing a password reset,
and the worker's two mail deliveries (slice 2.5); authorizing an administrator and changing a
user's role (slice 4.1). `results.py` holds `Authenticated`, the outcome the sign-in use cases
share, and `RoleChange`; `delivery_outcome.py` holds `DeliveryOutcome`, what both deliveries return."""
