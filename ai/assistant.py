"""AI assistant for Q&A and meeting summarization.

Supports Anthropic (Claude) and OpenAI (GPT) as interchangeable providers.
Provider and model are runtime-configurable via reload_client().
"""
import base64
import json
import re
import traceback
from typing import Callable

from core import log as log
# Importing config injects truststore (OS trust store) into Python's TLS stack,
# so provider clients created below trust corporate WARP's inspection CA. Kept
# explicit here so TLS works regardless of module import order.
from core import config as _config  # noqa: F401

Callback = Callable[[str], None]
ToolEventCallback = Callable[[str, dict], None]  # (event_type, payload) → None
FrameExtractor = Callable[[float], bytes | None]  # timestamp → JPEG bytes or None
# Generic tool executor: (tool_name, tool_input) → (content, is_error, summary, extra)
# content: str or list of blocks; is_error: bool; summary: str for UI; extra: optional dict (e.g. image)
ToolExecutor = Callable[[str, dict], tuple]

# Tool definition used for Anthropic structured patch output.
# Array-of-sections format so the model can create, rename, or restructure
# sections freely without being confined to a hardcoded set.
_PATCH_TOOL = {
    "name": "update_summary",
    "description": (
        "Update the meeting summary. Only return sections with genuinely new "
        "high-level content - do not update for minor details or topics already "
        "captured. Return an empty sections array if nothing significant changed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "description": "Sections to create or update. Omit sections that need no changes.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Section heading (no ## prefix). May be an existing or new section name.",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["append", "replace"],
                            "description": (
                                "'append': add new content to an existing section. "
                                "'replace': rewrite the section entirely, or create a new one."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Markdown content for this section (no ## heading). "
                                "For 'append': only new content not already present. "
                                "For 'replace': the complete consolidated content. "
                                "Nesting and sub-bullets are encouraged for clarity. "
                                "Timestamps: append [M:SS] after a bullet when it anchors a specific "
                                "decision, commitment, or notable moment - e.g. '- Agreed to delay launch [12:04]'. "
                                "Use [M:SS–M:SS] ranges to mark the span of a key topic or discussion block. "
                                "Do NOT timestamp every bullet - only moments worth jumping to."
                            ),
                        },
                    },
                    "required": ["name", "action", "content"],
                },
            }
        },
        "required": ["sections"],
        "additionalProperties": False,
    },
}

_SCREENSHOT_TOOL = {
    "name": "get_screenshot",
    "description": (
        "Capture a screenshot from the meeting's screen recording at a specific "
        "timestamp. Use this to see what was on screen at a given moment - "
        "useful for reading slides, shared documents, UI content, code, diagrams, "
        "or anything visual that the transcript alone cannot convey. "
        "You may call this multiple times with different timestamps."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "timestamp": {
                "type": "number",
                "description": (
                    "Time in seconds from the start of the recording. "
                    "Use timestamps from the transcript to target specific moments."
                ),
            },
        },
        "required": ["timestamp"],
    },
}

# OpenAI-compatible function definition for the same tool
_SCREENSHOT_FUNC_OAI = {
    "type": "function",
    "function": {
        "name": "get_screenshot",
        "description": _SCREENSHOT_TOOL["description"],
        "parameters": _SCREENSHOT_TOOL["input_schema"],
    },
}

# ── Native web search (server-side, executed by the provider) ────────────────
_WEB_SEARCH_ANTHROPIC = {"type": "web_search_20250305", "name": "web_search"}
_WEB_SEARCH_OAI       = {"type": "web_search_preview"}

# ── Global Chat tools ────────────────────────────────────────────────────────

_GLOBAL_TOOLS = [
    {
        "name": "search_transcripts",
        "description": (
            "Search across all meeting transcripts using keyword/full-text search. "
            "Returns matching snippets with session titles, IDs, and context. "
            "Use this for specific words, phrases, or names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query terms"},
                "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "semantic_search",
        "description": (
            "Search meetings by meaning and topic similarity. Better for conceptual "
            "queries like 'discussions about project deadlines' or 'feedback on the design' "
            "rather than exact words. Returns ranked session matches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Conceptual search query"},
                "limit": {"type": "integer", "description": "Max results (default 5)", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_session_detail",
        "description": (
            "Load the full transcript and summary of a specific meeting session. "
            "Use this after searching to get detailed context from a particular meeting. "
            "The transcript may be truncated for very long sessions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session ID to load"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "list_speakers",
        "description": (
            "List all known speakers from the Voice Library with their names, "
            "colors, and the number of sessions they appear in. Use this when "
            "the user asks about participants or specific people."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_speaker_history",
        "description": (
            "Get all meetings a specific speaker appeared in, with session titles, "
            "dates, summaries, folder info, and how many segments they spoke. "
            "Use this when the user asks about a specific person's involvement, "
            "what someone has discussed across meetings, or to find meetings "
            "featuring a particular participant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "speaker_name": {
                    "type": "string",
                    "description": "The speaker's name to look up (case-insensitive, partial match supported)",
                },
            },
            "required": ["speaker_name"],
        },
    },
    {
        "name": "list_recent_meetings",
        "description": (
            "List meetings from a time range, sorted by date (newest first). "
            "Use this to BROWSE the meeting library by date — e.g. 'meetings "
            "from last week', 'today's meetings', 'meetings between Apr 1 "
            "and Apr 14'. This is the right tool when the user wants an "
            "overview rather than a keyword search. Returns titles, IDs, "
            "dates, durations, speakers, folders, and truncated summaries. "
            "Follow up with `get_session_detail` to load the full transcript "
            "of any specific meeting from the list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "within_days": {
                    "type": "integer",
                    "description": (
                        "Limit to meetings from the last N days. Use this "
                        "for relative ranges (e.g. 7 for last week). Omit "
                        "or set to 0 to use start_date/end_date instead."
                    ),
                },
                "start_date": {
                    "type": "string",
                    "description": (
                        "ISO date (YYYY-MM-DD) for the earliest meeting to "
                        "include. Optional — combine with end_date for an "
                        "explicit range."
                    ),
                },
                "end_date": {
                    "type": "string",
                    "description": (
                        "ISO date (YYYY-MM-DD) for the latest meeting to "
                        "include. Defaults to now if omitted."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of meetings to return (default 30, max 200).",
                    "default": 30,
                },
            },
        },
    },
]

_GLOBAL_TOOLS_OAI = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in _GLOBAL_TOOLS
]

# Models that support Anthropic extended thinking
_ANTHROPIC_THINKING_MODELS = {
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250219",
    "claude-3-7-sonnet-20250219",
}


def _format_meta_block(meta: dict | None) -> str:
    """Build a human-readable metadata block from session metadata."""
    if not meta:
        return ""

    lines = ["Session metadata:"]

    if meta.get("title"):
        lines.append(f"  Title: {meta['title']}")

    if meta.get("is_live"):
        lines.append("  Status: LIVE - recording is in progress, transcript is growing in real time")
    else:
        lines.append("  Status: Completed recording")

    if meta.get("started_at"):
        lines.append(f"  Started: {meta['started_at']}")
    if meta.get("ended_at"):
        lines.append(f"  Ended: {meta['ended_at']}")

    if meta.get("duration"):
        lines.append(f"  Duration: {meta['duration']}")

    if meta.get("segment_count"):
        lines.append(f"  Transcript segments: {meta['segment_count']}")

    if meta.get("speakers"):
        lines.append(f"  Speakers ({len(meta['speakers'])}): {', '.join(meta['speakers'])}")

    source_parts = []
    if meta.get("has_desktop_audio"):
        source_parts.append("desktop/system audio")
    if meta.get("has_mic_audio"):
        source_parts.append("microphone")
    if source_parts:
        lines.append(f"  Audio sources: {', '.join(source_parts)}")

    if meta.get("custom_prompt"):
        lines.append(f"\n  User-provided context: {meta['custom_prompt']}")

    return "\n".join(lines)


class AIAssistant:

    _SYSTEM_QA = (
        "You are an intelligent meeting assistant. You are scoped to a SINGLE meeting "
        "session - the transcript, metadata, and summary provided below are your only "
        "source of truth. Do not reference or speculate about other meetings.\n\n"
        "## What you know\n"
        "- The full transcript of THIS session with speaker labels and timestamps\n"
        "- Who the speakers are and what they discussed in THIS meeting\n"
        "- The timeline and flow of the conversation\n"
        "- The current auto-generated summary (if one exists)\n"
        "- Whether the recording is live or completed\n\n"
        "## Transcript format\n"
        "Each line follows: [M:SS] [Speaker Name] spoken text\n"
        "- Timestamps mark when each segment was spoken\n"
        "- Speaker labels may be auto-generated (\"Speaker 1\") or user-assigned names\n"
        "- The transcript is machine-generated from audio, so expect minor transcription "
        "errors, missing punctuation, or misheard words - interpret charitably\n\n"
        "## How to respond\n"
        "- Answer questions directly and concisely using markdown formatting\n"
        "- When quoting or referencing specific moments, include the timestamp as [M:SS] "
        "so the user can jump to that point in the recording\n"
        "- If the user asks about something not discussed in this meeting, say so clearly\n"
        "- You can cross-reference the summary and transcript - e.g. if asked to elaborate "
        "on a summary bullet point, find the relevant transcript section\n"
        "- If the recording is live, keep in mind more content may arrive after your answer\n"
        "- When speakers are identified by name, use their names naturally in your response\n"
        "- For questions about who said what, be precise about speaker attribution\n\n"
        "## Timestamps\n"
        "Timestamps are rendered as interactive pills that let users jump to that "
        "moment in the recording. They MUST be in square brackets to render correctly.\n"
        "- Format: [M:SS] for a single moment, [M:SS-M:SS] for a range\n"
        "- ALWAYS wrap timestamps in square brackets - bare timestamps like 2:50 "
        "will NOT render as clickable pills. Write [2:50] instead.\n"
        "- Place the timestamp after the referenced text, not before\n"
        "- For multiple timespans: [18:31-19:48] [27:17-27:26]\n"
        "- Only timestamp moments worth jumping to - avoid tagging every sentence\n"
        "- Use exact timestamps from the transcript only. No tildes or "
        "approximations like [~17:30].\n\n"
        "- Always respond in English regardless of any foreign words or phrases in the transcript\n\n"
        "## Web search\n"
        "You have access to a web search tool. Use it **sparingly** and only when "
        "a search would genuinely add value — for example, clarifying industry-specific "
        "terminology, looking up a product or company mentioned in the meeting, or "
        "providing context on an external event referenced by a speaker. Your primary "
        "focus should always remain on the meeting transcript itself."
    )

    _SYSTEM_SUMMARY = (
        "You are a meeting summarization assistant. You produce clear, well-structured "
        "summaries from audio transcripts.\n\n"
        "## Important context\n"
        "- The transcript may be partial, incomplete, or still in progress - the recording "
        "could be live and ongoing, or the audio may have been cut off mid-sentence\n"
        "- Work with whatever content is available; never refuse because the transcript "
        "seems short or incomplete\n\n"
        "## Transcript format\n"
        "Each line follows: [M:SS] [Speaker Name] spoken text\n"
        "- Timestamps mark when each segment was spoken\n"
        "- Speaker labels may be auto-generated (\"Speaker 1\") or user-assigned names\n"
        "- The transcript is machine-generated, so expect minor errors - interpret charitably\n\n"
        "## Output format\n"
        "- Choose section headings that fit the content and context - do not use a fixed "
        "structure. Let the transcript and any user instructions guide what sections to create.\n"
        "- Use markdown (## headings, bullets, **bold**, nesting) for a scannable hierarchy\n"
        "- Attribute key points and decisions to speakers by name when identified\n\n"
        "## Timestamps\n"
        "Timestamps let users jump directly to moments in the recording - use them surgically.\n"
        "- Format: `[M:SS]` for a moment, `[M:SS–M:SS]` for a span (e.g. a topic block)\n"
        "- Place AFTER the relevant bullet or phrase, not at the start: "
        "`- Team agreed to cut scope for v1 [8:14]`\n"
        "- Good candidates: decisions and commitments, action items assigned to someone, "
        "notable quotes or turning points, topic transitions, key disagreements resolved\n"
        "- Skip timestamps on: generic observations, filler content, bullets that are already "
        "obvious from context, or anywhere one per section is already enough\n"
        "- For multiple seperate timespan moments, group each range in it's own set of square brackets (e.g. [18:31–19:48] [27:17–27:26])\n"
        "- Aim for 1–3 timestamps per section - enough to orient, not so many they lose meaning\n\n"
        "## Quality bar\n"
        "- Keep every section as concise as possible - rich but tight\n"
        "- Do not pad with obvious or low-value bullets; every line should earn its place\n"
        "- Prefer nested structure over long flat lists when topics have sub-points\n"
        "- Always write in English regardless of any foreign words or phrases in the transcript"
    )

    def __init__(self, provider: str = "anthropic", model: str = "claude-sonnet-4-6") -> None:
        self.provider = provider
        self.model = model
        self.client = self._make_client(provider)
        self._clients: dict[str, object] = {provider: self.client}

    def _make_client(self, provider: str):
        """Create the API client.  Returns None gracefully if no key is set."""
        try:
            # TLS is verified against the OS trust store (truststore, injected by
            # core.config), so corporate WARP's inspection CA is honoured without
            # disabling certificate checks. The SDKs read the API key from the
            # environment (ANTHROPIC_API_KEY / OPENAI_API_KEY).
            if provider == "openai":
                from openai import OpenAI
                return OpenAI()
            import anthropic
            return anthropic.Anthropic()
        except Exception as e:
            print(f"[ai] Could not initialise {provider} client: {e}")
            return None

    def _get_client(self, provider: str):
        """Return a cached client for the given provider, creating if needed."""
        if provider not in self._clients or self._clients[provider] is None:
            self._clients[provider] = self._make_client(provider)
        return self._clients[provider]

    def reload_client(self, provider: str | None = None, model: str | None = None) -> None:
        """Re-create the client, optionally changing provider and/or model."""
        if provider is not None:
            self.provider = provider
        if model is not None:
            self.model = model
        self.client = self._make_client(self.provider)
        self._clients[self.provider] = self.client

    def _resolve(self, provider: str | None, model: str | None) -> tuple:
        """Return (client, provider, model) using overrides or defaults."""
        p = provider or self.provider
        m = model or self.model
        c = self._get_client(p) if p != self.provider else self.client
        return c, p, m

    def ask(
        self,
        transcript: str,
        chat_history: list[dict],
        on_token: Callback,
        on_done: Callable[[], None] | None = None,
        meta: dict | None = None,
        cancel: "threading.Event | None" = None,
        frame_extractor: FrameExtractor | None = None,
        on_tool_event: ToolEventCallback | None = None,
        provider: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        """Stream an answer to the latest question in chat_history.

        If ``frame_extractor`` is provided, the model can call the
        ``get_screenshot`` tool to view what was on screen at a given
        timestamp.  The tool loop runs up to 5 iterations.
        """
        meta_block = _format_meta_block(meta)
        summary_block = ""
        if meta and meta.get("current_summary"):
            summary_block = (
                f"\n\nCurrent auto-generated summary:\n---\n"
                f"{meta['current_summary']}\n---"
            )

        # User-supplied override (per-session or global) takes precedence over
        # the built-in QA prompt. The transcript/meta/screen-recording blocks
        # are still appended below so the model always gets meeting context.
        system = (system_prompt.strip() if system_prompt and system_prompt.strip() else self._SYSTEM_QA)
        if frame_extractor:
            system += (
                "\n\n## Screen recording\n"
                "A screen recording is available for this session. You can call the "
                "`get_screenshot` tool with a timestamp (in seconds) to see what was "
                "on screen at that moment. Use this whenever the user asks about visual "
                "content (slides, code, UI, diagrams, shared screens, etc.) or when "
                "the transcript references something being shown on screen. You may "
                "call the tool multiple times with different timestamps to examine "
                "different moments.\n\n"
                "### Embedding screenshots in your response\n"
                "Each screenshot tool result includes a markdown image URL. **Always embed "
                "relevant screenshots inline in your response** using the provided markdown "
                "syntax: `![description](url)`. This lets the user see what you're "
                "describing without expanding the tool panel. Include screenshots at the "
                "point in your response where they're most relevant - e.g. right after "
                "describing what's shown on screen."
            )
        system += "\n\n"
        if meta_block:
            system += meta_block + "\n\n"
        system += (
            f"Meeting transcript:\n---\n"
            f"{transcript or '(No transcript yet - meeting may just be starting)'}"
            f"\n---"
            f"{summary_block}"
        )

        self._stream_with_tools(
            system, chat_history, on_token, on_done,
            cancel=cancel, frame_extractor=frame_extractor,
            on_tool_event=on_tool_event,
            provider=provider, model=model,
        )

    def summarize(
        self,
        transcript: str,
        on_token: Callback,
        on_done: Callable[[], None] | None = None,
        custom_prompt: str = "",
        meta: dict | None = None,
        provider: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        """Stream a structured meeting summary from a full transcript.

        ``system_prompt`` overrides the built-in ``_SYSTEM_SUMMARY`` when
        provided and non-empty. ``custom_prompt`` is still appended on top
        as additional user instructions.
        """
        if not transcript.strip():
            on_token("*No transcript available yet - start recording first.*")
            if on_done:
                on_done()
            return

        system = (
            system_prompt.strip()
            if system_prompt and system_prompt.strip()
            else self._SYSTEM_SUMMARY
        )
        meta_block = _format_meta_block(meta)
        if meta_block:
            system += f"\n\n{meta_block}"
        if custom_prompt.strip():
            system += f"\n\nAdditional user instructions:\n{custom_prompt.strip()}"

        prompt = (
            "Summarize this transcript. Choose section headings that fit the content "
            "and any instructions above - do not use a fixed structure.\n\n"
            f"Transcript:\n---\n{transcript}\n---"
        )
        # Summarization routes OpenAI through the Responses API; Anthropic
        # keeps the existing messages.stream path via _stream().
        try:
            from core.network import warp_disconnect
            warp_disconnect()
            client, prov, mdl = self._resolve(provider, model)
            if client is None:
                on_token(
                    f"\n\n*Error: No {prov.title()} API key configured. "
                    f"Add it in Settings.*"
                )
                return
            messages = [{"role": "user", "content": prompt}]
            if prov == "openai":
                self._stream_openai_responses(system, messages, on_token,
                                              client=client, model=mdl)
            else:
                self._stream_anthropic(system, messages, on_token,
                                       client=client, model=mdl)
        except Exception as e:
            on_token(f"\n\n*Error: {e}*")
        finally:
            if on_done:
                on_done()

    def patch_summary(
        self,
        existing_summary: str,
        transcript: str,
        custom_prompt: str = "",
        meta: dict | None = None,
        update_context: str = "",
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        """Incrementally update a summary using the full transcript.

        The model chooses per-section whether to append new bullets or replace
        the whole section (e.g. for consolidation/deduplication). Sections not
        returned are left untouched, so content can never be silently dropped.
        """
        if not transcript.strip() and not update_context.strip():
            return existing_summary

        meta_block = _format_meta_block(meta)
        meta_note = f"\n\n{meta_block}" if meta_block else ""
        custom_note = (
            f"\n\nAdditional user instructions:\n{custom_prompt.strip()}"
            if custom_prompt.strip() else ""
        )
        update_note = (
            f"\n\nAdditional update context:\n{update_context.strip()}"
            if update_context.strip() else ""
        )

        system_prompt = (
            "You update structured meeting summaries incrementally. You receive the "
            "current summary and the full transcript.\n\n"
            "## When to update\n"
            "ONLY update when genuinely new high-level concepts, decisions, or topics "
            "have been discussed. Do not update for minor elaborations, repetition, or "
            "continued discussion of topics already captured. If nothing significant is "
            "new, return an empty sections array.\n\n"
            "## How to update\n"
            "- 'append': add new content to an existing section\n"
            "- 'replace': rewrite a section entirely (consolidation, deduplication, or "
            "restructuring), or create a new section\n"
            "- Section names are free-form - rename, merge, or create sections as the "
            "content warrants. Let the transcript and user instructions guide structure.\n\n"
            "## Quality bar\n"
            "- Keep all sections as concise as possible - rich but tight\n"
            "- Do not arbitrarily append bullets; update existing ones when appropriate\n"
            "- Use markdown hierarchy and nesting to keep things organised\n"
            "- Timestamps: [M:SS] format (e.g. [4:32]) inline for key moments only\n"
            "- Attribute decisions and points to speakers by name when identified\n"
            "- Always write in English regardless of any foreign words or phrases in the transcript"
        )
        if custom_prompt.strip():
            system_prompt += (
                f"\n\nAdditional user instructions:\n{custom_prompt.strip()}"
            )
        user_prompt = (
            f"Update the summary to reflect any significant new content in the transcript."
            f"{meta_note}{custom_note}{update_note}\n\n"
            f"Current summary:\n---\n{existing_summary}\n---\n\n"
            f"Full transcript:\n---\n{transcript}\n---\n\n"
            f"Return a sections array with only the sections that need changes. "
            f"Each entry: name, action ('append'/'replace'), content (markdown, no ## heading). "
            f"Omit unchanged sections. Return empty sections array if nothing is new."
        )

        try:
            raw = self._complete_structured(system_prompt, user_prompt, provider=provider, model=model)
        except Exception as e:
            log.warn("summary", f"patch failed ({e}) - keeping existing summary")
            return existing_summary

        section_updates = raw.get("sections", []) if isinstance(raw, dict) else []
        if not section_updates:
            return existing_summary

        sections = self._parse_sections(existing_summary)
        updated: list[str] = []
        for item in section_updates:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "").strip()
            action = item.get("action", "append")
            content = str(item.get("content", "")).strip()
            if not name or not content:
                continue
            if action == "replace":
                sections[name] = content
            else:  # append
                existing = sections.get(name, "").strip()
                sections[name] = (existing + "\n\n" + content).strip() if existing else content
            updated.append(f"{name}({action})")

        if not updated:
            return existing_summary

        log.info("summary", f"Updated: {updated}")
        return self._build_summary(sections)

    _SYSTEM_GLOBAL_QA = (
        "You are an intelligent meeting assistant with access to a library of "
        "recorded meetings. You search across ALL sessions to answer questions - "
        "you are NOT scoped to any single meeting.\n\n"
        "## How to respond\n"
        "- Use your tools to find relevant information before answering - "
        "do not guess or make up content\n"
        "- **Cite every session you reference as a markdown link** so the user "
        "can open it in one click. Format: `[Meeting Title](/session?id=<session_id>)`. "
        "The `session_id` is included in every tool result. Example: \"In "
        "[Sprint Planning (Apr 7)](/session?id=abc-123-def), the team decided…\"\n"
        "- Reference speakers by name and note their involvement across sessions\n"
        "- Do NOT include [M:SS] timestamps - this is a cross-session view, "
        "not a single-recording player\n"
        "- If the user asks about something you can't find, say so clearly\n"
        "- Answer directly and concisely using markdown formatting\n"
        "- When multiple sessions are relevant, synthesize information across them "
        "and link to each one inline\n"
        "- For questions about who said what, be precise about speaker attribution "
        "and link to the meeting it was in\n"
        "- Always respond in English regardless of any foreign words or phrases "
        "in the transcripts\n\n"
        "## Tool usage strategy\n"
        "- Use `list_recent_meetings` when the user asks for a date-bounded "
        "browse (e.g. 'last week', 'today', 'this month', explicit dates) — "
        "this returns a chronological overview without needing keywords\n"
        "- Use `search_transcripts` for specific keywords or phrases\n"
        "- Use `semantic_search` for conceptual/thematic queries\n"
        "- Use `get_session_detail` to load full transcript + summary from "
        "a particular meeting (use after listing/searching to dig deeper)\n"
        "- Use `list_speakers` to see all known participants across all meetings\n"
        "- Use `get_speaker_history` to find all meetings a specific person "
        "appeared in, with their activity level in each\n"
        "- You may call tools multiple times to gather enough context. "
        "Combine tools freely — e.g. list recent meetings, then load "
        "details for the ones that look relevant.\n\n"
        "## Context in results\n"
        "- Search results include session summaries (truncated) so you can often "
        "answer without loading the full transcript\n"
        "- Results include folder names when sessions are organized into folders - "
        "use this to provide project/team context\n"
        "- Speaker history shows segment counts per session to indicate how "
        "active someone was in each meeting\n\n"
        "## Web search\n"
        "You also have access to a web search tool. Use it **sparingly** — only "
        "when a search would genuinely add value beyond the meeting data. Good "
        "uses: clarifying industry terms or acronyms mentioned in meetings, looking "
        "up a company or product referenced by a speaker, providing context on an "
        "external event. Your primary focus should always be the stored meetings."
    )

    def ask_global(
        self,
        chat_history: list[dict],
        on_token: Callback,
        on_done: Callable[[], None] | None = None,
        cancel: "threading.Event | None" = None,
        on_tool_event: ToolEventCallback | None = None,
        tool_executor: "ToolExecutor | None" = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Stream an answer for the Global Chat (cross-session Q&A)."""
        self._stream_with_tools(
            self._SYSTEM_GLOBAL_QA,
            chat_history,
            on_token,
            on_done,
            cancel=cancel,
            on_tool_event=on_tool_event,
            tools_anthropic=_GLOBAL_TOOLS,
            tools_openai=_GLOBAL_TOOLS_OAI,
            tool_executor=tool_executor,
            provider=provider, model=model,
        )

    _SYSTEM_TITLE = (
        "You generate ultra-short meeting titles. "
        "Reply with ONLY 2-4 words in Title Case. "
        "No punctuation, no quotes, no explanation.\n\n"
        "Guidance:\n"
        "- The transcript is the primary signal for what the meeting was about.\n"
        "- If past meeting titles are provided, infer the user's naming style "
        "(e.g. \"Product Standup\", \"Design Review\", \"1:1 with Alice\") and "
        "match it when the signals indicate a recurring series.\n"
        "- Meetings with the same participants AND similar day/time are "
        "almost certainly the same recurring meeting — reuse or closely "
        "mirror the existing title.\n"
        "- One-off meetings with unfamiliar participants should get a fresh, "
        "content-specific title.\n"
        "- Prefer specificity over generic words like \"Meeting\" or \"Call\"."
    )

    def generate_title(
        self,
        transcript: str,
        *,
        context: dict | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """Return a short title for the meeting, or '' on failure/no content.

        ``context`` (optional) may include:
          - ``started_at``: ISO timestamp for the current meeting.
          - ``participants``: ``[{'name': str, 'global_id': str|None}, ...]``
          - ``similar_past_meetings``: list of dicts with ``title``,
            ``shared_speakers``, ``same_dow``, ``hour_delta``. Already sorted
            by relevance (most-similar first).

        ``system_prompt`` (optional) overrides the built-in ``_SYSTEM_TITLE``.
        The participant / past-meeting context block is still appended to the
        user prompt regardless, so even custom prompts get the meeting context.

        When context is supplied the model is steered to match existing naming
        conventions for recurring meetings with the same participants / time.
        """
        if not transcript.strip():
            return ""
        snippet = transcript[:1000].strip()

        # ── Build the context block ─────────────────────────────────────────
        ctx_lines: list[str] = []
        if context:
            started_at = context.get("started_at")
            if started_at:
                try:
                    from datetime import datetime as _dt
                    dt = _dt.fromisoformat(started_at)
                    ctx_lines.append(
                        f"Meeting date: {dt.strftime('%A, %b %d %Y at %I:%M %p').lstrip('0')}"
                    )
                except Exception:
                    pass
            participants = context.get("participants") or []
            names = [p["name"] for p in participants if p.get("name")]
            if names:
                preview = ", ".join(names[:10])
                extra = f" (+{len(names) - 10} more)" if len(names) > 10 else ""
                ctx_lines.append(f"Participants: {preview}{extra}")

            past = context.get("similar_past_meetings") or []
            if past:
                ctx_lines.append("")
                ctx_lines.append(
                    "Past meeting titles from this user, most similar first "
                    "(similarity based on shared participants, same day-of-week, "
                    "and similar time-of-day):"
                )
                for m in past[:8]:
                    sig_parts = []
                    ss = int(m.get("shared_speakers") or 0)
                    if ss:
                        sig_parts.append(f"{ss} shared participant{'s' if ss != 1 else ''}")
                    if m.get("same_dow"):
                        sig_parts.append("same day-of-week")
                    hd = m.get("hour_delta")
                    if hd is not None:
                        if hd < 0.25:
                            sig_parts.append("same time-of-day")
                        else:
                            sig_parts.append(f"~{hd:.1f}h time offset")
                    tag = f"  [{'; '.join(sig_parts)}]" if sig_parts else ""
                    ctx_lines.append(f'- "{m["title"]}"{tag}')

        context_block = "\n".join(ctx_lines)

        # ── System + user messages ─────────────────────────────────────────
        system = (
            system_prompt.strip()
            if system_prompt and system_prompt.strip()
            else self._SYSTEM_TITLE
        )
        user_parts = []
        if context_block:
            user_parts.append(context_block)
            user_parts.append("")
        user_parts.append(f"Transcript excerpt:\n{snippet}")
        user_parts.append("")
        user_parts.append("Title:")
        user_msg = "\n".join(user_parts)

        try:
            raw = self._complete(system, user_msg)
            # Strip quotes / punctuation the model sometimes emits despite instructions
            cleaned = raw.strip().strip('"\'`').rstrip(".!?,:;")
            words = cleaned.split()[:4]
            return " ".join(words)
        except Exception:
            return ""

    # ── Anthropic prompt caching ─────────────────────────────────────────────

    @staticmethod
    def _build_cached_kwargs(
        system: str,
        messages: list[dict],
        model: str,
        max_tokens: int = 4096,
        tools: list | None = None,
        extra: dict | None = None,
    ) -> dict:
        """Build Anthropic request kwargs with prompt-caching breakpoints.

        Places two ``cache_control`` markers per request:

        1. End of the stable prefix — the last tool definition (or the system
           block when there are no tools).  Caches system + tool schemas.
        2. End of the message history — the last content block of the last
           message.  Creates a rolling cache of conversation context.

        Reads cost ~10% of input tokens; writes cost ~125%.  The 5-min TTL
        refreshes on each hit.
        """
        _CC = {"type": "ephemeral"}

        system_blocks = [{"type": "text", "text": system}]
        kwargs = {"model": model, "system": system_blocks, "max_tokens": max_tokens}
        if extra:
            kwargs.update(extra)

        # ── Breakpoint 1: stable prefix (tools or system) ──────────────
        if tools:
            cached_tools = [dict(t) for t in tools]
            cached_tools[-1] = {**cached_tools[-1], "cache_control": _CC}
            kwargs["tools"] = cached_tools
        else:
            system_blocks[-1] = {**system_blocks[-1], "cache_control": _CC}

        # ── Breakpoint 2: end of accumulated message history ───────────
        cached_msgs: list[dict] = []
        for msg in messages:
            new_msg = dict(msg)
            content = msg.get("content")
            if isinstance(content, list):
                new_msg["content"] = [
                    dict(b) if isinstance(b, dict) else b
                    for b in content
                ]
            cached_msgs.append(new_msg)

        for msg in reversed(cached_msgs):
            content = msg.get("content")
            if isinstance(content, list) and content:
                last = content[-1]
                if isinstance(last, dict):
                    content[-1] = {**last, "cache_control": _CC}
                break
            elif isinstance(content, str) and content:
                msg["content"] = [{"type": "text", "text": content, "cache_control": _CC}]
                break

        kwargs["messages"] = cached_msgs
        return kwargs

    # ── Internal ──────────────────────────────────────────────────────────────

    def _stream(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        on_done: Callable[[], None] | None,
        cancel: "threading.Event | None" = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Stream tokens from the active provider."""
        try:
            from core.network import warp_disconnect
            warp_disconnect()
            client, prov, mdl = self._resolve(provider, model)
            if client is None:
                on_token(
                    f"\n\n*Error: No {prov.title()} API key configured. "
                    f"Add it in Settings.*"
                )
                return
            if prov == "openai":
                self._stream_openai(system, messages, on_token, cancel, client=client, model=mdl)
            else:
                self._stream_anthropic(system, messages, on_token, cancel, client=client, model=mdl)
        except Exception as e:
            on_token(f"\n\n*Error: {e}*")
        finally:
            if on_done:
                on_done()

    def _stream_anthropic(self, system: str, messages: list[dict], on_token: Callback,
                           cancel: "threading.Event | None" = None,
                           client=None, model: str | None = None) -> None:
        import anthropic
        c = client or self.client
        m = model or self.model
        extra: dict = {}
        if m in _ANTHROPIC_THINKING_MODELS:
            extra["thinking"] = {"type": "adaptive"}
        api_kwargs = self._build_cached_kwargs(system, messages, m, extra=extra)
        try:
            with c.messages.stream(**api_kwargs) as stream:
                for text in stream.text_stream:
                    if cancel and cancel.is_set():
                        stream.close()
                        break
                    on_token(text)
        except anthropic.AuthenticationError:
            on_token("\n\n*Error: Invalid Anthropic API key. Check Settings.*")
        except anthropic.RateLimitError:
            on_token("\n\n*Error: Anthropic rate limit reached. Please wait and retry.*")

    @staticmethod
    def _to_openai_messages(messages: list[dict]) -> list[dict]:
        """Convert Anthropic-style content blocks to OpenAI vision format."""
        out = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                out.append(m)
                continue
            # content is a list of blocks - convert to OpenAI format
            parts: list[dict] = []
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    parts.append({"type": "text", "text": block["text"]})
                elif btype == "image":
                    src = block.get("source", {})
                    mime = src.get("media_type", "image/png")
                    b64 = src.get("data", "")
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
            out.append({"role": m["role"], "content": parts or content})
        return out

    @staticmethod
    def _to_responses_input(messages: list[dict]) -> list[dict]:
        """Convert internal message list to OpenAI Responses API input items."""
        out: list[dict] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            text_type = "input_text" if role == "user" else "output_text"
            if isinstance(content, str):
                out.append({"role": role, "content": [{"type": text_type, "text": content}]})
                continue
            parts: list[dict] = []
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    parts.append({"type": text_type, "text": block["text"]})
                elif btype == "image":
                    src = block.get("source", {})
                    mime = src.get("media_type", "image/png")
                    b64 = src.get("data", "")
                    parts.append({"type": "input_image", "image_url": f"data:{mime};base64,{b64}"})
            out.append({"role": role, "content": parts})
        return out

    @staticmethod
    def _convert_tools_for_responses(tools: list[dict]) -> list[dict]:
        """Flatten chat.completions function tool shape to Responses API shape."""
        out: list[dict] = []
        for t in tools:
            if t.get("type") == "function" and "function" in t:
                fn = t["function"]
                out.append({
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                })
            else:
                out.append(t)
        return out

    def _stream_openai(self, system: str, messages: list[dict], on_token: Callback,
                        cancel: "threading.Event | None" = None,
                        client=None, model: str | None = None) -> None:
        import openai
        c = client or self.client
        m = model or self.model
        converted = self._to_openai_messages(messages)
        full_messages = [{"role": "system", "content": system}] + converted
        try:
            stream = c.chat.completions.create(
                model=m,
                #max_tokens=4096,
                messages=full_messages,
                stream=True,
            )
            for chunk in stream:
                if cancel and cancel.is_set():
                    stream.close()
                    break
                content = chunk.choices[0].delta.content
                if content:
                    on_token(content)
        except openai.AuthenticationError:
            on_token("\n\n*Error: Invalid OpenAI API key. Check Settings.*")
        except openai.RateLimitError:
            on_token("\n\n*Error: OpenAI rate limit reached. Please wait and retry.*")

    def _stream_openai_responses(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        cancel: "threading.Event | None" = None,
        client=None,
        model: str | None = None,
    ) -> None:
        """Stream tokens from OpenAI's Responses API.

        Used by ``summarize()`` for OpenAI models so summarization runs go
        through ``/v1/responses`` instead of Chat Completions.
        """
        import openai
        c = client or self.client
        m = model or self.model
        try:
            stream_ctx = c.responses.stream(
                model=m,
                instructions=system,
                input=self._to_responses_input(messages),
            )
        except openai.AuthenticationError:
            on_token("\n\n*Error: Invalid OpenAI API key. Check Settings.*")
            return
        except openai.RateLimitError:
            on_token("\n\n*Error: OpenAI rate limit reached. Please wait and retry.*")
            return
        except Exception as e:
            log.error("ai", f"OpenAI responses.stream rejected request on model {m!r}: {e}")
            on_token(
                f"\n\n*OpenAI rejected the request: {e}. The model "
                f"({m}) may not support the Responses API. "
                f"Try a different model in Settings.*"
            )
            return

        try:
            with stream_ctx as stream:
                for event in stream:
                    if cancel and cancel.is_set():
                        stream.close()
                        break
                    etype = getattr(event, "type", "")
                    if etype == "response.output_text.delta":
                        delta = getattr(event, "delta", "") or ""
                        if delta:
                            on_token(delta)
        except openai.AuthenticationError:
            on_token("\n\n*Error: Invalid OpenAI API key. Check Settings.*")
        except openai.RateLimitError:
            on_token("\n\n*Error: OpenAI rate limit reached. Please wait and retry.*")

    def _complete(self, system: str, prompt: str, max_tokens: int = 1024,
                   provider: str | None = None, model: str | None = None) -> str:
        """Non-streaming single completion from the active provider."""
        from core.network import warp_disconnect
        warp_disconnect()
        client, prov, mdl = self._resolve(provider, model)
        if client is None:
            raise RuntimeError(
                f"No {prov.title()} API key configured. Add it in Settings."
            )
        if prov == "openai":
            response = client.chat.completions.create(
                model=mdl,
                #max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            return response.choices[0].message.content.strip()
        else:
            response = client.messages.create(
                model=mdl,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()

    def _complete_structured(self, system: str, prompt: str,
                              provider: str | None = None, model: str | None = None) -> dict:
        """Structured completion returning a dict with section arrays.

        Anthropic: uses tool use so the SDK enforces the schema.
        OpenAI: uses json_object response format + prompt instructions.
        Returns {} on empty or unparseable responses.
        """
        from core.network import warp_disconnect
        warp_disconnect()
        client, prov, mdl = self._resolve(provider, model)
        if client is None:
            raise RuntimeError(
                f"No {prov.title()} API key configured. Add it in Settings."
            )
        if prov == "openai":
            # Responses API with a JSON-schema text format for the
            # patch-summary contract. Mirrors _PATCH_TOOL.input_schema.
            response = client.responses.create(
                model=mdl,
                instructions=system,
                input=[
                    {"role": "user",
                     "content": [{"type": "input_text", "text": prompt}]},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "summary_patch",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "sections": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "name":    {"type": "string"},
                                            "action":  {"type": "string", "enum": ["append", "replace"]},
                                            "content": {"type": "string"},
                                        },
                                        "required": ["name", "action", "content"],
                                    },
                                },
                            },
                            "required": ["sections"],
                        },
                    },
                },
            )
            text = (response.output_text or "").strip()
            return json.loads(text) if text else {}
        else:
            response = client.messages.create(
                model=mdl,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[_PATCH_TOOL],
                tool_choice={"type": "tool", "name": _PATCH_TOOL["name"]},
            )
            for block in response.content:
                if block.type == "tool_use":
                    return block.input or {}
            return {}

    # ── Tool-use streaming ──────────────────────────────────────────────────────

    def _stream_with_tools(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        on_done: Callable[[], None] | None,
        cancel: "threading.Event | None" = None,
        frame_extractor: FrameExtractor | None = None,
        on_tool_event: ToolEventCallback | None = None,
        tools_anthropic: list | None = None,
        tools_openai: list | None = None,
        tool_executor: "ToolExecutor | None" = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Stream with tool-use loop (up to 50 iterations).

        Accepts either a ``frame_extractor`` (legacy screenshot-only mode)
        or generic ``tool_executor`` + tool lists for arbitrary tool handling.
        """
        try:
            from core.network import warp_disconnect
            warp_disconnect()
            client, prov, mdl = self._resolve(provider, model)
            if client is None:
                on_token(
                    f"\n\n*Error: No {prov.title()} API key configured. "
                    f"Add it in Settings.*"
                )
                return
            a_tools = (tools_anthropic or [_SCREENSHOT_TOOL]) + [_WEB_SEARCH_ANTHROPIC]
            o_tools = (tools_openai or [_SCREENSHOT_FUNC_OAI]) + [_WEB_SEARCH_OAI]
            if prov == "openai":
                self._tool_loop_openai(
                    system, messages, on_token, cancel, frame_extractor,
                    on_tool_event=on_tool_event,
                    tools=o_tools, tool_executor=tool_executor,
                    client=client, model=mdl,
                )
            else:
                self._tool_loop_anthropic(
                    system, messages, on_token, cancel, frame_extractor,
                    on_tool_event=on_tool_event,
                    tools=a_tools, tool_executor=tool_executor,
                    client=client, model=mdl,
                )
        except Exception as e:
            on_token(f"\n\n*Error: {e}*")
        finally:
            if on_done:
                on_done()

    def _execute_tool_anthropic(
        self,
        tu: dict,
        frame_extractor: FrameExtractor | None,
        tool_executor: "ToolExecutor | None",
        on_tool_event: ToolEventCallback | None,
    ) -> dict:
        """Execute a single tool call and return an Anthropic tool_result block."""
        # Notify frontend about the tool call.  The id is the Anthropic
        # tool_use_id, which the frontend uses to pair tool_result events with
        # their originating tool_call — required when tools execute in
        # parallel and results return out of order.
        tu_id = tu["id"]
        if on_tool_event:
            on_tool_event("tool_call", {"id": tu_id, "name": tu["name"], "input": tu["input"]})

        # Try generic executor first
        if tool_executor:
            try:
                content, is_error, summary, extra = tool_executor(tu["name"], tu["input"])
                result = {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": content,
                }
                if is_error:
                    result["is_error"] = True
                if on_tool_event:
                    payload = {"id": tu_id, "name": tu["name"], "success": not is_error, "summary": summary}
                    if extra:
                        payload.update(extra)
                    on_tool_event("tool_result", payload)
                return result
            except Exception:
                pass  # fall through to built-in handlers

        # Built-in: get_screenshot
        if tu["name"] == "get_screenshot" and frame_extractor:
            ts = tu["input"].get("timestamp", 0)
            result = frame_extractor(float(ts))
            # frame_extractor returns (jpeg_bytes, url) or just jpeg_bytes for compat
            if result:
                if isinstance(result, tuple):
                    jpeg, url = result
                else:
                    jpeg, url = result, None
                b64 = base64.b64encode(jpeg).decode()
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": tu_id, "name": tu["name"], "success": True,
                        "summary": f"Captured screenshot at {ts:.1f}s", "image": b64,
                    })
                # Tell the model the image URL so it can embed it in markdown
                text_msg = f"Screenshot at {ts:.1f}s captured."
                if url:
                    text_msg += f" Embed in your response with: ![Screenshot at {ts:.1f}s]({url})"
                return {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": [
                        {"type": "text", "text": text_msg},
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/jpeg", "data": b64,
                        }},
                    ],
                }
            else:
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": tu_id, "name": tu["name"], "success": False,
                        "summary": "Could not extract frame",
                    })
                return {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": "Could not extract frame - the timestamp may be out of range or no video is available.",
                    "is_error": True,
                }

        # Unknown tool
        if on_tool_event:
            on_tool_event("tool_result", {
                "id": tu_id, "name": tu["name"], "success": False,
                "summary": f"Unknown tool: {tu['name']}",
            })
        return {
            "type": "tool_result",
            "tool_use_id": tu["id"],
            "content": f"Unknown tool: {tu['name']}",
            "is_error": True,
        }

    def _tool_loop_anthropic(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        cancel: "threading.Event | None",
        frame_extractor: FrameExtractor | None,
        on_tool_event: ToolEventCallback | None = None,
        tools: list | None = None,
        tool_executor: "ToolExecutor | None" = None,
        client=None,
        model: str | None = None,
    ) -> None:
        import anthropic

        c = client or self.client
        m = model or self.model
        msgs = list(messages)  # working copy
        max_rounds = 50
        a_tools = tools or [_SCREENSHOT_TOOL]

        had_text = False  # track if any text was emitted before tools
        for _ in range(max_rounds):
            if cancel and cancel.is_set():
                return

            extra: dict = {}
            if m in _ANTHROPIC_THINKING_MODELS:
                extra["thinking"] = {"type": "adaptive"}
            api_kwargs = self._build_cached_kwargs(
                system, msgs, m, tools=a_tools, extra=extra,
            )

            round_had_text = False
            with c.messages.stream(**api_kwargs) as stream:
                # Event-based iteration so we can detect server tools
                # (native web search) in real time alongside text.
                for event in stream:
                    if cancel and cancel.is_set():
                        stream.close()
                        return
                    if event.type == "text":
                        if not round_had_text and had_text:
                            on_token("\n\n---\n\n")
                        round_had_text = True
                        on_token(event.text)
                    elif event.type == "content_block_start":
                        cb = event.content_block
                        if getattr(cb, "type", "") == "server_tool_use" and on_tool_event:
                            on_tool_event("tool_call", {
                                "id": getattr(cb, "id", None),
                                "name": getattr(cb, "name", "web_search"),
                                "input": getattr(cb, "input", {}),
                            })
                response = stream.get_final_message()

            # Emit tool_result events for any server-side tools (web search).
            # The id pairs each result with its originating server_tool_use
            # block above — necessary when several searches run in parallel.
            if on_tool_event:
                for block in response.content:
                    if getattr(block, "type", "") == "web_search_tool_result":
                        content = getattr(block, "content", [])
                        n = sum(
                            1 for c in (content if isinstance(content, list) else [])
                            if getattr(c, "type", "") == "web_search_result"
                        )
                        on_tool_event("tool_result", {
                            "id": getattr(block, "tool_use_id", None),
                            "name": "web_search",
                            "success": True,
                            "summary": f"Found {n} result{'s' if n != 1 else ''}",
                        })

            if round_had_text:
                had_text = True

            tool_uses: list[dict] = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_uses.append({
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            if not tool_uses or response.stop_reason != "tool_use":
                return

            msgs.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tu in tool_uses:
                tool_results.append(self._execute_tool_anthropic(
                    tu, frame_extractor, tool_executor, on_tool_event,
                ))

            msgs.append({"role": "user", "content": tool_results})

    def _execute_tool_openai(
        self,
        call_id: str,
        tc_name: str,
        tc_args_raw: str,
        msgs: list[dict],
        frame_extractor: FrameExtractor | None,
        tool_executor: "ToolExecutor | None",
        on_tool_event: ToolEventCallback | None,
    ) -> None:
        """Execute a single OpenAI tool call, appending Responses API items to msgs."""
        try:
            parsed_args = json.loads(tc_args_raw)
        except Exception as parse_err:
            log.warn("ai", f"OpenAI tool {tc_name!r} bad JSON args: {parse_err} -- raw={tc_args_raw!r}")
            parsed_args = {}

        if on_tool_event:
            on_tool_event("tool_call", {"id": call_id, "name": tc_name, "input": parsed_args})

        # Try generic executor first
        if tool_executor:
            try:
                content, is_error, summary, extra = tool_executor(tc_name, parsed_args)
                result_text = content if isinstance(content, str) else json.dumps(content)
                msgs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result_text,
                })
                if on_tool_event:
                    payload = {"id": call_id, "name": tc_name, "success": not is_error, "summary": summary}
                    if extra:
                        payload.update(extra)
                    on_tool_event("tool_result", payload)
                return
            except Exception as exec_err:
                # Don't silently swallow — surface the error to both the
                # model (so it can correct course) and the server log.
                log.error("ai", f"OpenAI tool {tc_name!r} executor raised: {exec_err}")
                traceback.print_exc()
                err_text = f"Tool {tc_name!r} failed: {exec_err}"
                msgs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": err_text,
                })
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": call_id, "name": tc_name, "success": False, "summary": err_text,
                    })
                return

        # Built-in: get_screenshot
        if tc_name == "get_screenshot" and frame_extractor:
            ts = float(parsed_args.get("timestamp", 0))
            result = frame_extractor(ts)
            if result:
                if isinstance(result, tuple):
                    jpeg, url = result
                else:
                    jpeg, url = result, None
                b64 = base64.b64encode(jpeg).decode()
                text_msg = f"Screenshot at {ts:.1f}s captured."
                if url:
                    text_msg += f" Embed in your response with: ![Screenshot at {ts:.1f}s]({url})"
                msgs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": text_msg,
                })
                # function_call_output.output is text-only; provide the image as a
                # follow-up user input so vision models can ground their answer.
                msgs.append({
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
                    ],
                })
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": call_id, "name": tc_name, "success": True,
                        "summary": f"Captured screenshot at {ts:.1f}s", "image": b64,
                    })
            else:
                msgs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": "Could not extract frame - the timestamp may be out of range or no video is available.",
                })
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": call_id, "name": tc_name, "success": False, "summary": "Could not extract frame",
                    })
            return

        # Unknown tool
        msgs.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": f"Unknown tool: {tc_name}",
        })
        if on_tool_event:
            on_tool_event("tool_result", {
                "id": call_id, "name": tc_name, "success": False, "summary": f"Unknown tool: {tc_name}",
            })

    def _tool_loop_openai(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        cancel: "threading.Event | None",
        frame_extractor: FrameExtractor | None,
        on_tool_event: ToolEventCallback | None = None,
        tools: list | None = None,
        tool_executor: "ToolExecutor | None" = None,
        client=None,
        model: str | None = None,
    ) -> None:
        c = client or self.client
        m = model or self.model
        o_tools = self._convert_tools_for_responses(tools or [_SCREENSHOT_FUNC_OAI])
        max_rounds = 50

        next_input = self._to_responses_input(messages)
        previous_response_id: str | None = None
        had_text = False
        tool_names = sorted({t.get("name") or t.get("type") for t in o_tools if t})
        log.info("ai", f"OpenAI tool loop on model={m!r}, tools={tool_names}")

        for round_idx in range(max_rounds):
            if cancel and cancel.is_set():
                return

            kwargs = {
                "model": m,
                "instructions": system,
                "input": next_input,
                "tools": o_tools,
            }
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id

            function_calls: list[dict] = []
            web_search_seen = False
            round_had_text = False
            cancelled = False

            try:
                stream_ctx = c.responses.stream(**kwargs)
            except Exception as e:
                log.error("ai", f"OpenAI responses.stream rejected request on model {m!r}: {e}")
                on_token(
                    f"\n\n*OpenAI rejected the request: {e}. The model "
                    f"({m}) may not support tool use via the Responses API. "
                    f"Try a different model in Settings.*"
                )
                return

            with stream_ctx as stream:
                for event in stream:
                    if cancel and cancel.is_set():
                        cancelled = True
                        stream.close()
                        break
                    etype = getattr(event, "type", "")
                    if etype == "response.output_text.delta":
                        delta = getattr(event, "delta", "") or ""
                        if delta:
                            if not round_had_text and had_text:
                                on_token("\n\n---\n\n")
                            round_had_text = True
                            on_token(delta)
                    elif etype == "response.output_item.done":
                        item = getattr(event, "item", None)
                        itype = getattr(item, "type", "") if item is not None else ""
                        if itype == "function_call":
                            function_calls.append({
                                "call_id": getattr(item, "call_id", "") or "",
                                "name": getattr(item, "name", "") or "",
                                "arguments": getattr(item, "arguments", "") or "",
                            })
                        elif itype == "web_search_call" and not web_search_seen and on_tool_event:
                            web_search_seen = True
                            ws_id = getattr(item, "id", "") or "openai-websearch"
                            on_tool_event("tool_call", {"id": ws_id, "name": "web_search", "input": {}})
                            on_tool_event("tool_result", {
                                "id": ws_id,
                                "name": "web_search",
                                "success": True,
                                "summary": "Web search performed",
                            })
                    elif etype == "response.error" or etype == "error":
                        err = getattr(event, "error", None) or getattr(event, "message", None)
                        log.error("ai", f"OpenAI Responses stream error event: {err}")

                if cancelled:
                    return
                final = stream.get_final_response()
                previous_response_id = getattr(final, "id", None) or previous_response_id

            if round_had_text:
                had_text = True

            log.info(
                "ai",
                f"OpenAI round {round_idx}: {len(function_calls)} function call(s), "
                f"text={'yes' if round_had_text else 'no'}",
            )

            if not function_calls:
                # Diagnostic: model gave a final answer without using tools at
                # all. If the user expected tool use this is the smoking gun.
                if round_idx == 0 and tool_executor and tool_names:
                    log.warn(
                        "ai",
                        f"OpenAI model {m!r} produced a response without "
                        f"calling any of the provided tools: {tool_names}. "
                        f"If tool use was expected, verify the model "
                        f"supports the Responses API + function calling.",
                    )
                return

            next_input = []
            for call in function_calls:
                self._execute_tool_openai(
                    call["call_id"], call["name"], call["arguments"], next_input,
                    frame_extractor, tool_executor, on_tool_event,
                )

    # ── Summary helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_sections(summary: str) -> dict[str, str]:
        """Parse a ## markdown summary into {section_name: content} dict."""
        sections: dict[str, str] = {}
        current: str | None = None
        lines: list[str] = []
        for line in summary.splitlines():
            m = re.match(r"^##\s+(.+)$", line)
            if m:
                if current is not None:
                    sections[current] = "\n".join(lines).strip()
                current = m.group(1).strip()
                lines = []
            elif current is not None:
                lines.append(line)
        if current is not None:
            sections[current] = "\n".join(lines).strip()
        return sections

    @staticmethod
    def _build_summary(sections: dict[str, str]) -> str:
        """Rebuild markdown from a sections dict, preserving insertion order."""
        return "\n\n".join(
            f"## {name}\n{content}"
            for name, content in sections.items()
            if content.strip()
        )
