---
name: assistant-agent-interview-trainer
description: Project-local simulated technical interview workflow for assistant_agent. Use when the user asks to continue an interview, run mock interview practice, ask module interview questions, grade or improve their answer, provide correct standard answers, produce interview golden sentences, or update docs/interview notes for context engineering, tool calling, memory service, agent communication, or other assistant_agent modules.
---

# Assistant Agent Interview Trainer

Use this skill to run the project's interview-training workflow without duplicating the repository's interview documentation. The repository remains the source of truth; this skill is the execution wrapper.

## Start

1. Locate the project root.
   - Prefer the current working directory when it contains `AGENTS.md` and `docs/interview/README.md`.
   - If those files are absent, ask for the `assistant_agent` repository path before updating files.
2. Read `AGENTS.md`.
3. Read `docs/interview/README.md` completely.
4. Identify the target module from the user's request, conversation context, or active interview directory.
5. If the module has a current architecture authority, read it before producing standard answers:
   - Context engineering: `docs/CONTEXT_ENGINEERING_STATUS.md`.
   - Tool calling: `docs/tool-calling-architecture.md`.
   - Memory service: `docs/memory-service-architecture.md`.
   - Agent communication: `docs/agent-communication-routing.md`.
6. Inspect relevant source and tests before citing project implementation details.

Do not treat `docs/development/**` as the current answer source unless the user explicitly asks for historical decisions.

## Interview Loop

When the user says "continue", "继续", or asks for the next question:

1. Pick the next unanswered question for the active module.
2. Label it as:
   - 🔴 必考题: core, high-frequency, must answer clearly.
   - 🟡 高频题: common follow-up, should connect to project details.
   - 🟢 加分题: differentiator, usually about tradeoffs, boundaries, production risk, or debugging.
3. Ask only the question. Let the user answer first.
4. Do not provide the standard answer until after the user has answered, unless they explicitly ask for direct explanation mode.

When the user answers:

1. Preserve the user's answer faithfully for documentation.
2. Give interview-style feedback in this order:
   - What was correct.
   - What was incomplete, risky, or inaccurate.
   - A correct standard answer that can be spoken in an interview.
   - Interview golden sentence(s).
   - Project code locations and test locations.
3. Keep the feedback practical and technically precise.
4. Prefer concrete project evidence over generic agent-system theory.

## Documentation Updates

If the user is in interview-documentation mode, or has not opted out of documentation updates, update all applicable files after each completed question:

1. Module `index.md`: add or update the question entry, user-answer summary, core points, and detail link.
2. Module `details/NN-slug.md`: write the full question, user's original answer, feedback, standard answer, golden sentence, and code locations.
3. Module `cheat-sheet.md`: add concise keywords, key path, common pitfall, and golden sentence.
4. `docs/interview/README.md`: update module question count/status when needed.

Use the structure defined in `docs/interview/README.md`. Do not invent a second directory layout.

If the user explicitly says not to edit files, answer in chat only and mention that no interview files were updated.

## Module Routing

Use these directory conventions unless the repository README says otherwise:

- Context engineering: `docs/interview/context_engineering_interview/`.
- Tool calling: `docs/interview/tool_calling_interview/`.
- Memory service: `docs/interview/memory_service_interview/`.
- Agent communication: `docs/interview/agent_communication_interview/`.

For a new module:

1. Create `docs/interview/{module}_interview/index.md`.
2. Create `docs/interview/{module}_interview/cheat-sheet.md`.
3. Create `docs/interview/{module}_interview/details/`.
4. Add the module to `docs/interview/README.md`.

## Answer Quality Rules

- Prefer correctness over friendliness when correcting the user's answer.
- Explain why an answer is interview-safe or risky.
- Tie abstract concepts back to repository boundaries, file names, and tests.
- Do not cite stale roadmap/development files as current design.
- Do not make up code locations; search first if unsure.
- Avoid long generic essays. Standard answers should be deep enough to pass an interview but structured for spoken delivery.

## Validation

For documentation-only updates, run:

```bash
git diff --check -- docs/interview
```

If AGENTS or other docs are updated, include them in `git diff --check`.

For code-behavior changes made during an interview task, run the smallest relevant pytest subset from the module authority document or `AGENTS.md`.
