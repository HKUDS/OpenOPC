"""Role-level skill mounting: SkillLibrary.build_skills_summary honours skill_refs.

Roadmap #1 — a role mounts a subset of the skill library via its ``skill_refs``.
Empty skill_refs keeps the prior "offer everything" behaviour (backward compat);
a non-empty set scopes the optional skills; ``always`` skills stay global.
"""

from __future__ import annotations

import unittest

from opc.layer5_memory.skill_library import Skill, SkillLibrary


def _library(skills: list[Skill]) -> SkillLibrary:
    lib = SkillLibrary.__new__(SkillLibrary)
    lib._skills = {s.name: s for s in skills}
    return lib


def _skills() -> list[Skill]:
    return [
        Skill(name="physical_ai", description="robotics domain", content="ROBO", source_path="core/physical_ai.md"),
        Skill(name="coding", description="code well", content="CODE", source_path="core/coding.md"),
        Skill(name="writing", description="write well", content="WRITE", source_path="core/writing.md"),
        # A global always-on skill (not the special-cased "memory" name).
        Skill(name="house_rules", description="always on", always=True, content="RULES", source_path="core/house_rules.md"),
    ]


class SkillRefsScopingTests(unittest.TestCase):
    def test_empty_skill_refs_offers_all_optional_skills(self) -> None:
        out = _library(_skills()).build_skills_summary(execution_mode="company_mode", role_id="r")
        self.assertIn("- **physical_ai**", out)
        self.assertIn("- **coding**", out)
        self.assertIn("- **writing**", out)

    def test_skill_refs_scopes_to_the_mounted_subset(self) -> None:
        out = _library(_skills()).build_skills_summary(
            execution_mode="company_mode", role_id="r", skill_refs=["physical_ai"]
        )
        self.assertIn("- **physical_ai**", out)
        self.assertNotIn("- **coding**", out)
        self.assertNotIn("- **writing**", out)

    def test_always_skill_ignores_skill_refs_scoping(self) -> None:
        # house_rules is not in skill_refs but is always-on -> still injected.
        out = _library(_skills()).build_skills_summary(
            execution_mode="company_mode", role_id="r", skill_refs=["physical_ai"]
        )
        self.assertIn("## Skill: house_rules", out)
        self.assertIn("RULES", out)

    def test_blank_and_whitespace_refs_are_ignored(self) -> None:
        # A skill_refs list of only blanks behaves like "no scoping".
        out = _library(_skills()).build_skills_summary(
            execution_mode="company_mode", role_id="r", skill_refs=["", "  "]
        )
        self.assertIn("- **coding**", out)


if __name__ == "__main__":
    unittest.main()
