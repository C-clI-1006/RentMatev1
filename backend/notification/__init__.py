"""Notification component (shared capability).

Implements `contracts.INotificationService` plus the dispatcher (queue
worker) which is the SINGLE retry owner. Delivery statuses are truthful:
accepted / pending_review / delivered / failed / pending_retry.
"""
