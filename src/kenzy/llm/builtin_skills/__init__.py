"""Built-in skills bundled with Kenzy.

The ``.py`` files in this package are the default ``@skill`` / ``@fast_intent``
implementations. They are discovered by path-scan at ``kenzy-llm`` startup (see
``kenzy.llm.skills.load_skills``), the same mechanism used for user skills, so
a user file in the config-home ``skills/`` directory that defines a skill of the
same name overrides the built-in one. This ``__init__`` exists only so the skill
modules ship inside the wheel; it is skipped by the loader (``_``-prefixed).
"""
