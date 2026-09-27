"""
SENTRA — Core layer.

This package is the heart of the robot:
  - core.state   : single source of truth for robot mode / e-stop / motor state
  - core.safety  : the ONLY gate through which motor commands may pass
  - core.config  : (future) runtime configuration

Rule: services never touch motor GPIO directly and never bypass core.safety.
"""
