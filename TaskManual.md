# Task Routing Guide

## What This Guide Covers

This guide explains how to write tasks in a way that ClawMind can understand reliably.

ClawMind is designed to turn a Logseq task into a controlled workflow, not just a one-off chat prompt.  
The clearer your task is, the better ClawMind can choose the right execution path and produce a traceable result.

## Supported Task Languages

ClawMind supports task text written in:

- Traditional Chinese
- Simplified Chinese
- English

You can write tasks in the language that feels most natural for your workflow.

## How ClawMind Routes Tasks

ClawMind does not treat every task the same way.

In general:

- straightforward requests are handled with a faster default model path
- reasoning-heavy requests can be routed to a deeper analysis path
- structured update tasks can be routed to a more deterministic execution path

When `LLM_BRAND=gemini_api`, ClawMind uses the Gemini API path instead of the Codex CLI path. In that mode, simpler requests are typically routed to `gemini-2.5-flash`, while heavier reasoning tasks can be routed to `gemini-2.5-pro`.

ClawMind also considers whether a task looks like multi-dimensional analysis. In practice, this means the router gives extra weight to requests that combine comparison or decision wording with multiple evaluation angles, such as tradeoffs across several factors. Internally, this behaves like a `multi_analysis_bonus_score`: it is an extra routing signal, not the only rule.

This allows the system to balance speed, reasoning depth, and writeback stability.

## What Makes a Good Task

A strong task usually has these qualities:

- a clear goal
- enough context to understand the request
- an output expectation that is easy to verify

Good examples:

- “Compare these two approaches and explain the tradeoffs.”
- “Summarize this page and give me a short conclusion.”
- “Help me update this note into a cleaner final version.”

Less effective examples:

- “Handle this.”
- “Fix it.”
- “Do something with this note.”

## When ClawMind Uses Deeper Reasoning

ClawMind is more likely to use a deeper reasoning path when a task asks for:

- comparison
- explanation
- recommendation
- tradeoff analysis
- synthesis across multiple pages or references

If a task is more analytical, more ambiguous, or spans more context, ClawMind will tend to treat it as a deeper reasoning problem.

## Writing Tips

- Write the task as a concrete request, not just a topic.
- If you want comparison or evaluation, say that explicitly.
- If the task depends on multiple pages, link them clearly.
- If you want a specific kind of output, describe it directly.

## Mental Model

You can think of ClawMind as choosing between three broad paths:

- fast answer path
- deeper reasoning path
- deterministic structured writeback path

You do not need to memorize internal routing rules to use it well.  
The main rule is simple: write tasks clearly, and write them in the form of the outcome you want.