"""Review component (shared capability).

Manager review workflow: show original message + source facts + draft +
reason; record decisions immutably (contracts.ReviewDecisionRecord);
approval binds to a specific draft version; rejection => no tenant
message.
"""
