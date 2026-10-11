"""dotdeployer - declarative, transactional dotfile deployment.

Public API::

    from dotdeployer import Config, Project, plan, apply

Everything is implemented on top of the Python standard library only, so a
single ``pip install dotdeployer`` (or even dropping the ``src`` directory on
``PYTHONPATH``) is enough to run it on Linux, macOS and Windows.
"""

from __future__ import annotations

__all__ = [
    "Config",
    "Plan",
    "Project",
    "ProjectError",
    "__version__",
    "apply",
    "plan",
    "rollback",
]

__version__ = "1.0.0"
__author__ = "hrfrutrd-dev"
__license__ = "MIT"

from .config import Config
from .errors import DotDeployerError as ProjectError
from .project import Plan, Project, apply, plan, rollback
