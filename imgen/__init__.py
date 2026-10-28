"""Google Flow bulk image generator — core engine (UI-independent)."""

#: The single source of the version number.
#:
#: pyproject reads it from here rather than carrying its own copy, because
#: two copies of a fact drift: the shipped defaults did exactly that and
#: a fresh install lost a whole section of settings before anyone noticed.
#: The updater compares this against the newest GitHub release, so inside
#: a frozen bundle - where pyproject.toml does not exist - it has to be
#: here, in the code.
__version__ = "1.1.1"
