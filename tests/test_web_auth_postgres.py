"""Legacy marker for the pre-split backend PostgreSQL test module.

Cloud PostgreSQL coverage lives with the standalone ``cloud`` package.  The
old backend integration tests are intentionally not collected because the
local backend must remain database-independent.
"""

import pytest

pytest.skip("PostgreSQL authentication now belongs to the standalone cloud package.", allow_module_level=True)
