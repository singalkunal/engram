"""Load analysis prompts from external files."""

import pathlib

_PROMPT_DIR = pathlib.Path(__file__).parent / "prompts"

COMMON_INSTRUCTIONS = (_PROMPT_DIR / "common_instructions.txt").read_text()
PER_SESSION = (_PROMPT_DIR / "per_session.txt").read_text()
CROSS_SESSION = (_PROMPT_DIR / "cross_session.txt").read_text()
