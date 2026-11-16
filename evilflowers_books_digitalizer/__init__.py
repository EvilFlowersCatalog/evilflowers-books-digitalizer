"""EvilFlowers Books Digitalizer: portal, orchestration and lab of book digitalization.

* ``studio``    the portal (SPA + API) and the orchestrator (job ledger, scheduling)
* ``pipeline``, ``reflow``, ``covers``, ``metadata``, ``catalog``  domain libraries the
  in-process stages still use (they move to the services: docs/design/target-architecture.md)
* ``cli``       ``evilflowers-digitalizer``: the studio commands and the batch commands

Nothing is imported here on purpose: an API or worker process loads only what it uses.
"""

__version__ = "0.1.0"
