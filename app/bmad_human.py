"""
The "AI Human": answers Aider's mid-coding questions on the user's
behalf, using the epic's own BMad artifacts as its only source of
truth (no invented context) — reuses the same LLMRouter/env vars
(LLM_PROVIDER, ANTHROPIC_API_KEY / DEEPSEEK_API_KEY) already wired for
pipeline runs, so no separate setup needed.
"""
from app.llm_router import llm_router

BMAD_HUMAN_PERSONA_TEMPLATE = """You are standing in for the human product owner on a software project.
A coding assistant (Aider) is implementing one epic and has asked you a
question mid-implementation. Answer AS the human decision-maker —
directly, concretely, in 1-4 sentences. Do not hedge, do not say "it
depends" without picking a default. Ground your answer in the project
knowledge base below (it includes the specific epic being worked on,
marked PRIMARY EPIC, plus the BRD/analysis/architecture as reference).
If it genuinely doesn't cover the question, make the most reasonable
product decision a competent PO would make and state it as a decision,
not a suggestion.

# Project knowledge base:
{epic_context}
"""


def answer_question(epic_context: str, question: str) -> str:
    persona = BMAD_HUMAN_PERSONA_TEMPLATE.format(epic_context=epic_context[:20000])
    return llm_router.run(persona, question)
