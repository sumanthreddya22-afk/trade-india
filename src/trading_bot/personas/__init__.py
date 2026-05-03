"""Named expert personas for LLM committees.

Each persona is a versioned system prompt that grounds an LLM in a real-
world practitioner role (forensic short-seller, sell-side analyst, CRO,
etc.). Persona-grounded prompts produce reasoning vocabulary and biases
identifiable in audit trails — and the lesson loop (Phase D) tracks
per-persona accuracy so the right voices get more weight over time.

Each persona file exports:
  PROMPT: the persona system prompt string
  VERSION: short version tag (e.g. "v1") used in the audit row's
           ``prompt_version`` column

Versioning rule: bump VERSION when the prompt changes meaningfully so
historical analysis can compare apples-to-apples.
"""
from __future__ import annotations
