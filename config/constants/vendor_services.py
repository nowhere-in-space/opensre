"""Whether this build may contact the services behind the product.

Four things reach a vendor host on their own in the local CLI path: product
analytics, error reporting, the account check the interactive shell runs at
startup, and the release check behind ``update`` and ``doctor``. None is asked
for by the operator, and all four fail badly in the network this build runs in -
inside a customer's cloud, where those hosts are unreachable and "nothing leaves
this machine" is a condition of being allowed to run at all.

The account check is the sharpest of the four: it treats an unreachable app as
"not signed in", so a blocked host means no interactive shell at all.

A constant rather than an environment variable, because the guarantee has to
hold for someone who never read the documentation. Consulted in five places
rather than deleted at hundreds of call sites, because this tree is rebased onto
upstream regularly and one flag conflicts far less than scattered deletions.
"""

from __future__ import annotations

from typing import Final

#: False here means: this build never contacts a vendor service by itself. The
#: integrations the operator configured, and the LLM they chose, are untouched -
#: those are the work, not reporting on it.
VENDOR_SERVICES_ENABLED: Final[bool] = False

__all__ = ["VENDOR_SERVICES_ENABLED"]
