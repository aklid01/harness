"""Pins between the engine's contracts and the renderer's hand-typed copies.

protocol.ts mirrors shapes the engine owns: the mode names, the review
stages and the model fields. A change on either side that forgets the other
is invisible to both typecheckers, so this file compares them directly.
"""

import re
import unittest
from pathlib import Path

from harness import providers
from harness.review.engine import STAGES

PROTOCOL = (
    Path(__file__).resolve().parent.parent
    / "desktop"
    / "web"
    / "shared"
    / "protocol.ts"
)


def type_members(name):
    text = PROTOCOL.read_text(encoding="utf-8")
    match = re.search(rf"export type {name} =\s*([^;]+);", text)
    if match is None:
        raise AssertionError(f"protocol.ts no longer declares type {name}")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def interface_keys(name):
    text = PROTOCOL.read_text(encoding="utf-8")
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"protocol.ts no longer declares interface {name}")
    return set(re.findall(r"^  (\w+)\??:(?: |$)", match.group(1), re.MULTILINE))


class RendererContract(unittest.TestCase):
    def test_mode_names_match_the_engine(self):
        self.assertEqual(type_members("Mode"), {"build", "ask", "plan"})

    def test_every_engine_review_stage_is_declared_in_the_protocol(self):
        emitted = set(STAGES) | {"updated"}
        missing = emitted - type_members("ReviewStage")
        self.assertEqual(
            missing, set(), f"stages the renderer never expects: {missing}"
        )

    def test_model_entry_matches_the_provider_model_fields(self):
        self.assertEqual(
            interface_keys("ModelEntry"), {"id"} | set(providers.MODEL_FIELDS)
        )


if __name__ == "__main__":
    unittest.main()
