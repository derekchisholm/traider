"""Pulumi entry point. See ``stack.py`` for what gets created."""

import pulumi

from stack import build

for name, value in build().outputs.items():
    pulumi.export(name, value)
