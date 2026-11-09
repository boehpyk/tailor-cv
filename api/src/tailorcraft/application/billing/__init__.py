"""Use cases for the `billing` bounded context (slice 5.2): open an account, charge and refund a run,
read the credits, grant some, record a pack click, and choose the payment notice.

One module per user intent, each with a frozen command. **No commit and no logger here**: every use
case runs on the caller's transaction (plan §0.3, §0.4). `ChargeCreditForRun` and `RefundCreditForRun`
**return** their events instead of publishing them, because each is composed into another context's
use case, which publishes after its own aggregate's events.
"""
