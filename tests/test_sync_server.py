"""Legacy marker for the removed centralized sync server.

Snapshot HTTP contract coverage belongs to the cloud API and the local
``HttpCloudSnapshotRepository`` adapter.  The old shared sync server would
reintroduce the centralized backend boundary.
"""

import pytest

pytest.skip("The centralized sync server was replaced by cloud snapshot HTTP APIs.", allow_module_level=True)
