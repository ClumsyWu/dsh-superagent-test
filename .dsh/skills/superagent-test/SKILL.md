---
name: superagent-test
description: Run validation tests for dsh-superagent features on this test repository, including Skill, MCP, and SDK integration verification.
---

# superagent-test

This skill provides reusable task-specific instructions for testing dsh-superagent features against this repository. It is defined as a project-level skill in `.dsh/skills/` so the dsh-superagent filesystem provider discovers and loads it automatically.

## Purpose

Use this skill when working on this test repository (`dsh-superagent-test`) to validate that dsh-superagent correctly discovers, loads, and executes skills. It serves as both a test fixture and a demonstration of the skill system.

## Structure

Skills in this repository follow the DSH skill convention:

```
.dsh/skills/<skill-name>/
  └── SKILL.md    # YAML frontmatter + Markdown body
```

The `SKILL.md` file contains:
- **YAML frontmatter** with `name` (kebab-case identifier), `description` (routing description), and optional `whenToUse`
- **Skill instructions** in Markdown, used as the `<skill_content>` body when the skill is loaded

## Invocation

This skill is `modelInvocable` and `userInvocable`. The model may call the `skill` tool to load it; a human may invoke it through the user-facing command surface. The loaded skill body is rendered as a `<skill_content>` block with `name="superagent-test"`.

## Verification

To verify the skill system is working:
1. List available skills via the catalog
2. Load this skill by name `superagent-test`
3. Confirm the loaded content matches this file's body