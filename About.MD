harmony_agent_cli.py is an interactive, multi-turn terminal agent built on top of OpenAI Harmony and Ollama, designed to inspect, edit, and run local bash scripts directly on your system through autonomous tool calling.

Core Architecture & Mechanics
Harmony Protocol & Native Channeling: Uses openai-harmony to encode conversations and parse model completions across distinct channels—separating internal reasoning ([analysis]), tool calls ([commentary]), and user responses ([final]).

Multi-Step Agent Loop: When you issue a prompt, the agent enters an autonomous inner cycle (up to 8 steps). It can call a tool, inspect the output, call another tool, and synthesize a response before handing control back to you.

Token Streaming & Parameter Tuning: Connects directly to Ollama’s raw generation endpoint (/api/generate) with live chunk streaming, an expanded context window (num_ctx: 8192), and a high generation limit (num_predict: 4096) to prevent long scripts from getting cut off mid-write.

Model Discovery: Dynamically queries http://localhost:11434/api/tags on startup to detect and bind to the running model (defaulting to your pinned Harmony model).

Registered Local ToolsToolActionSafety / Behaviorread_fileReads file contents from disk into context.Resolves tilde (~) paths, checks existence.write_fileCreates or overwrites complete scripts.Automatically generates a .bak backup first; sets chmod +x on .sh files.patch_filePerforms targeted search-and-replace edits.Creates a .bak backup; verifies snippet presence before editing.run_shell_commandExecutes commands in bash.Captures stdout, stderr, and return codes with a 30s timeout (ideal for bash -n syntax checks).

Key Behavioral Guardrails

Enforced Action over Chat: The developer system prompt instructs the model to apply modifications directly to disk via tool calls rather than outputting raw Markdown code blocks into the chat.

Authenticated Tool Authoring: Passes tool results back into the Harmony conversation using explicit Author(Role.TOOL, "functions.<name>") wrappers, satisfying Harmony's Rust-core schema requirements.