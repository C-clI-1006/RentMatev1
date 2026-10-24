"""Evidence module (owner: Jenny).

Responsibilities (guide Ch.16): upload -> classification -> association
(upload success != classification/association success), timeline, lease
info extraction boundaries, event consumption.

First increment (SCRUM-8) implemented: upload with validation and
storage, plus property-scoped list / get / download. `router.py` is
mounted in `main.py`; prototype header identity in the router is
TEST-ONLY, not real authentication.
"""
